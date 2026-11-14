#!/usr/bin/env python3
"""GitHub AI Radar — standard-library-only collector and static site builder."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import gzip
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOTS = ROOT / "data" / "snapshots"
REPORTS = ROOT / "reports"
SITE = ROOT / "site"
API = "https://api.github.com"
UA = "github-ai-radar/1.0 (+https://github.com)"
TOPICS = json.loads((ROOT / "config" / "topics.json").read_text(encoding="utf-8"))
CAT_RULES = {
    "Coding Agent": ["coding-agent", "ai-coding", "code-agent", "coding assistant", "code assistant", "aider", "code generation", "code review", "programming assistant", "software engineer agent"],
    "AI Agent": ["ai-agent", "ai-agents", "agentic-ai", "agent framework", "multi-agent", "autonomous agent", "agent workflow", "agentic", "agent"],
    "MCP": ["mcp", "model-context-protocol", "model context protocol"],
    "LLM / Models": ["llm", "large-language-model", "large language model", "transformer", "language model", "foundation model", "multimodal", "generative-ai"],
    "RAG / Data": ["rag", "retrieval augmented", "retrieval-augmented", "vector-database", "knowledge base", "embedding", "vector search"],
    "Inference / Local": ["inference", "local-llm", "gguf", "mlx", "ollama", "vllm", "quantization", "model serving"],
    "AI Developer Tools": ["ai-tools", "developer-tools", "devtools", "cli", "sdk", "ai-assistant", "prompt-engineering", "ai editor", "developer tools"],
    "ML / Research": ["machine-learning", "deep-learning", "pytorch", "computer vision", "image generation", "diffusion", "speech recognition", "research"],
}

def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def http_get(url: str, *, token: str | None = None, json_api: bool = True) -> bytes:
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json" if json_api else "text/html"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as e:
            retryable = e.code in (429, 500, 502, 503, 504) or (e.code == 403 and e.headers.get("Retry-After"))
            if not retryable or attempt == 4:
                raise
            wait = min(60, int(e.headers.get("Retry-After", 0) or 0) or (2 ** attempt) * 3)
            print(f"HTTP {e.code}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("unreachable")


class TrendingParser(HTMLParser):
    """Best-effort parser; GitHub does not promise a stable Trending HTML schema."""
    def __init__(self):
        super().__init__()
        self.in_article = 0
        self.in_h2 = 0
        self.repos: set[str] = set()

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "article" and "Box-row" in d.get("class", ""):
            self.in_article += 1
        if self.in_article and tag == "h2":
            self.in_h2 += 1
        if self.in_article and self.in_h2 and tag == "a":
            path = d.get("href", "")
            if re.fullmatch(r"/[\w.-]+/[\w.-]+", path):
                self.repos.add(path.strip("/"))

    def handle_endtag(self, tag):
        if tag == "h2" and self.in_h2:
            self.in_h2 -= 1
        if tag == "article" and self.in_article:
            self.in_article -= 1
            self.in_h2 = 0


def discover_trending() -> set[str]:
    repos: set[str] = set()
    for window in ("daily", "weekly", "monthly"):
        try:
            parser = TrendingParser()
            parser.feed(http_get(f"https://github.com/trending?since={window}", json_api=False).decode("utf-8", "replace"))
            repos |= parser.repos
        except Exception as e:
            print(f"Trending {window} unavailable: {e}", file=sys.stderr)
    return repos


def categorize(repo: dict) -> list[str]:
    topics = " ".join(repo.get("topics") or []).lower().replace("_", "-")
    name = (repo.get("name") or "").lower()
    desc = (repo.get("description") or "").lower()
    content = f"{topics} {name} {desc}"
    categories = []
    for group, terms in CAT_RULES.items():
        if any(term in content for term in terms):
            categories.append(group)
    return categories or ["AI Developer Tools"]


def normalize(repo: dict) -> dict:
    return {
        "full_name": repo["full_name"],
        "url": repo.get("html_url", "https://github.com/" + repo["full_name"]),
        "description": (repo.get("description") or "")[:340],
        "stars": int(repo.get("stargazers_count") or 0),
        "forks": int(repo.get("forks_count") or 0),
        "open_issues": int(repo.get("open_issues_count") or 0),
        "created_at": repo.get("created_at", ""),
        "pushed_at": repo.get("pushed_at", ""),
        "archived": bool(repo.get("archived")),
        "categories": categorize(repo),
        "topics": (repo.get("topics") or [])[:30],
    }


def fetch_search(query: str, token: str) -> list[dict]:
    params = urllib.parse.urlencode({"q": query, "per_page": str(TOPICS["max_per_search"]), "sort": "stars", "order": "desc"})
    payload = json.loads(http_get(f"{API}/search/repositories?{params}", token=token))
    if payload.get("incomplete_results"):
        print(f"WARNING: incomplete search: {query}", file=sys.stderr)
    return payload.get("items", [])


def collect(today: date | None = None) -> Path:
    today = today or utc_today()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required for data collection")
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    repos: dict[str, dict] = {}
    recent = (today - timedelta(days=30)).isoformat()
    failures = []
    for topic in TOPICS["topics"]:
        queries = (
            f"topic:{topic} fork:false archived:false stars:>={TOPICS['min_stars_established']}",
            f"topic:{topic} fork:false archived:false created:>={recent} stars:>={TOPICS['min_stars_new']}",
        )
        for query in queries:
            try:
                for item in fetch_search(query, token):
                    if not item.get("archived") and not item.get("fork") and not item.get("disabled"):
                        repos[item["full_name"].lower()] = normalize(item)
            except Exception as exc:
                failures.append(f"{query}: {exc}")
            time.sleep(2.1)  # GitHub Search API authenticated limits (<=30/min)
    # Refresh a bounded number of previously tracked repositories not found by
    # today's searches. NEVER copy yesterday's star count into today's snapshot.
    prev = latest_snapshot(before=today)
    if prev:
        missing = [r for r in prev["repositories"] if r["full_name"].lower() not in repos]
        for old in sorted(missing, key=lambda r: -r["stars"])[:250]:
            try:
                name = old["full_name"]
                data = json.loads(http_get(f"{API}/repos/{urllib.parse.quote(name, safe='/')}", token=token))
                if not data.get("archived") and not data.get("fork") and not data.get("disabled"):
                    repos[data["full_name"].lower()] = normalize(data)
            except Exception as exc:
                print(f"Could not refresh {old['full_name']}: {exc}", file=sys.stderr)
            time.sleep(0.22)
    # Trending is discovery-only, not a substitute for historical star snapshots.
    trending = discover_trending()
    for name in sorted(trending):
        key = name.lower()
        if key in repos:
            repos[key]["trending"] = True
            continue
        try:
            data = json.loads(http_get(f"{API}/repos/{urllib.parse.quote(name, safe='/')}", token=token))
            if not data.get("archived") and not data.get("fork"):
                normalized = normalize(data)
                if any(x.lower() in (" ".join(data.get("topics") or []) + " " + (data.get("description") or "")).lower() for x in ("ai", "llm", "agent", "machine learning", "mcp", "model", "inference", "coding", "rag", "ml")):
                    normalized["trending"] = True
                    repos[key] = normalized
        except Exception as exc:
            print(f"Skipped Trending repository {name}: {exc}", file=sys.stderr)
    if not repos:
        raise RuntimeError("No repositories collected; refusing to write an empty snapshot. " + "; ".join(failures[:2]))
    if failures and len(failures) >= len(TOPICS["topics"]):
        raise RuntimeError(f"Too many GitHub search failures ({len(failures)}); refusing incomplete collection")
    snapshot = {
        "date": today.isoformat(),
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "search_failures": failures,
        "repositories": sorted(repos.values(), key=lambda r: (-r["stars"], r["full_name"].lower())),
    }
    output = SNAPSHOTS / f"{today.isoformat()}.json.gz"
    with gzip.open(output, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(snapshot, f, ensure_ascii=False, separators=(",", ":"))
    for old in SNAPSHOTS.glob("*.json.gz"):
        try:
            when = date.fromisoformat(old.name[:-8])
            if (today - when).days > TOPICS["snapshot_retention_days"]:
                old.unlink()
        except ValueError:
            continue
    print(f"Snapshot: {output.relative_to(ROOT)} | repositories={len(repos)} | failures={len(failures)}")
    return output


def load_snapshot(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def latest_snapshot(before: date | None = None) -> dict | None:
    files = sorted(SNAPSHOTS.glob("????-??-??.json.gz"), reverse=True)
    for file in files:
        d = date.fromisoformat(file.name[:-8])
        if before is None or d < before:
            return load_snapshot(file)
    return None


def snapshot_on(when: date) -> dict | None:
    f = SNAPSHOTS / f"{when.isoformat()}.json.gz"
    return load_snapshot(f) if f.exists() else None


def is_recently_active(repo: dict, today: date, *, days: int = 180) -> bool:
    try:
        return (today - date.fromisoformat(repo["pushed_at"][:10])).days <= days
    except (ValueError, TypeError, KeyError):
        return False


def build_boards(current: dict, week: dict | None, month: dict | None, limit: int = 50) -> dict:
    now = date.fromisoformat(current["date"])
    historic = []
    for snapshot in (week, month):
        historic.append({r["full_name"].lower(): r for r in snapshot["repositories"]} if snapshot else {})
    boards: dict[str, list[dict]] = {"week": [], "month": [], "established": [], "potential": []}
    for r in current["repositories"]:
        if r.get("archived"):
            continue
        item = dict(r)
        for name, old, baseline in (("delta7", historic[0], week), ("delta30", historic[1], month)):
            prior = old.get(r["full_name"].lower())
            # Require the correct calendar-day baseline; never invent missing historical data.
            item[name] = max(0, item["stars"] - int(prior["stars"])) if baseline and prior else None
        try:
            age = (now - date.fromisoformat(item["created_at"][:10])).days
        except (ValueError, KeyError):
            age = 100000
        item["age_days"] = age
        item["active"] = is_recently_active(item, now)
        if item["delta7"] is not None and item["delta7"] > 0:
            boards["week"].append(item)
        if item["delta30"] is not None and item["delta30"] > 0:
            boards["month"].append(item)
        if item["stars"] >= 1000 and item["active"]:
            boards["established"].append(item)
        if age <= 90 and item["stars"] >= 20 and item["active"]:
            boards["potential"].append(item)
    boards["week"].sort(key=lambda r: (-r["delta7"], -r["stars"]))
    boards["month"].sort(key=lambda r: (-r["delta30"], -r["stars"]))
    boards["established"].sort(key=lambda r: (-r["stars"], -int(r["delta30"] or 0)))
    # Potential: show fast recent growth when available; otherwise use total stars and explicitly flag missing history.
    boards["potential"].sort(key=lambda r: (-(r["delta7"] or 0), -r["stars"]))
    return {k: v[:limit] for k, v in boards.items()}


def report_data() -> dict:
    curr = latest_snapshot()
    if not curr:
        return {
            "date": utc_today().isoformat(),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repository_count": 0,
            "baseline7": False,
            "baseline30": False,
            "search_failures": [],
            "boards": {"week": [], "month": [], "established": [], "potential": []},
        }
    today = date.fromisoformat(curr["date"])
    week = snapshot_on(today - timedelta(days=7))
    month = snapshot_on(today - timedelta(days=30))
    return {
        "date": curr["date"],
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repository_count": len(curr["repositories"]),
        "baseline7": week is not None,
        "baseline30": month is not None,
        "search_failures": curr.get("search_failures", []),
        "boards": build_boards(curr, week, month, TOPICS["top_per_board"]),
    }


def esc(s: object) -> str:
    return html.escape(str(s or ""), quote=True)


def card(r: dict, position: int) -> str:
    categories = " ".join(f'<span class="tag">{esc(c)}</span>' for c in r["categories"][:3])
    d7 = f'+{r["delta7"]:,}' if r.get("delta7") is not None else "待积累"
    d30 = f'+{r["delta30"]:,}' if r.get("delta30") is not None else "待积累"
    return (f'<article class="repo" data-search="{esc((r["full_name"]+" "+r["description"]+" "+" ".join(r["categories"])).lower())}" '
            f'data-categories="{esc("|".join(r["categories"]))}">'
            f'<div class="repo-top"><span class="rank">#{position}</span>'
            f'<a href="{esc(r["url"])}" target="_blank" rel="noopener noreferrer">{esc(r["full_name"])}</a></div>'
            f'<p>{esc(r["description"] or "暂无简介")}</p><div class="tags">{categories}</div>'
            f'<div class="stats"><span>★ {r["stars"]:,}</span><span>7 天 {d7}</span><span>30 天 {d30}</span>'
            f'<span>Fork {r["forks"]:,}</span></div></article>')


def render_html(report: dict, *, archive: bool = False, archives: list[str] | None = None) -> str:
    labels = {"week": "7 天新锐榜", "month": "30 天新锐榜", "established": "成熟高人气榜", "potential": "潜力项目榜"}
    tabs = "".join(f'<button class="tab" type="button" data-tab="{key}" aria-selected="{str(i == 0).lower()}">{label} <b>{len(report["boards"][key])}</b></button>' for i, (key, label) in enumerate(labels.items()))
    panels = "".join(f'<section class="panel" data-panel="{key}" {"" if i==0 else "hidden"}>' +
                     ("".join(card(r, idx) for idx, r in enumerate(report["boards"][key], 1)) or '<p class="empty">暂无符合条件的项目，或历史增长数据仍在积累中。</p>') + '</section>'
                     for i, key in enumerate(labels))
    archive_links = "".join(f'<a href="{("../" if archive else "") + "reports/" + esc(name) + ".html"}">{esc(name)}</a>' for name in (archives or []))
    notes = []
    if report["repository_count"] == 0:
        notes.append("当前尚无采集数据。请先运行每日采集工作流。")
    if not report["baseline7"]:
        notes.append("7 天基线数据尚未积累完整。")
    if not report["baseline30"]:
        notes.append("30 天基线数据尚未积累完整。")
    if report["search_failures"]:
        notes.append(f'本次采集有 {len(report["search_failures"])} 条查询失败，结果可能不完整。')
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub AI Radar — 全球 AI 开源热榜</title><meta name="description" content="全球 AI、Agent、Coding Agent、MCP、LLM、CLI 项目新锐和成熟榜单，按 Star 历史快照计算增长。">
<style>
:root{{--bg:#0b1220;--card:#172238;--line:#2b3b51;--muted:#9fb0c6;--text:#edf3fc;--accent:#8db7ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.55 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
a{{color:#b7d1ff;text-decoration:none}}a:hover{{text-decoration:underline}}.wrap{{max-width:1120px;margin:auto;padding:28px 20px 60px}}
header{{padding:28px 0 24px;border-bottom:1px solid var(--line)}}h1{{font-size:clamp(26px,5vw,42px);margin:0 0 8px;letter-spacing:-.03em}}header p{{color:var(--muted);margin:0 0 16px}}
.summary{{display:flex;flex-wrap:wrap;gap:10px}}.summary span{{border:1px solid var(--line);border-radius:20px;padding:5px 12px;color:#d8e6fd;font-size:13px}}
.notice{{padding:12px 16px;margin:18px 0;background:#30291b;border:1px solid #6b5632;border-radius:9px;color:#ffe0a3}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0 14px}}input,select{{background:var(--card);border:1px solid var(--line);border-radius:10px;color:var(--text);padding:11px 12px;font:inherit}}
input{{flex:2;min-width:220px}}select{{flex:1;min-width:180px}}.tabs{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}}.tab{{cursor:pointer;background:var(--card);color:var(--muted);border:1px solid var(--line);border-radius:9px;padding:10px 14px;font:inherit}}
.tab[aria-selected=true]{{border-color:var(--accent);color:#fff;background:#203350}}.tab b{{margin-left:5px;font-size:12px}}
.panel{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px}}.panel[hidden]{{display:none}}
.repo{{min-width:0;background:var(--card);border:1px solid var(--line);border-radius:13px;padding:17px;display:flex;flex-direction:column;gap:10px}}
.repo-top{{display:flex;gap:9px;align-items:baseline;word-break:break-word;font-weight:650}}.repo-top a{{font-size:16px}}.rank{{color:var(--muted);font-size:13px;white-space:nowrap}}
.repo p{{color:#b9c6d8;margin:0;min-height:44px;word-break:break-word}}.tags{{display:flex;gap:5px;flex-wrap:wrap}}.tag{{font-size:11px;background:#25344e;color:#b9dcff;border-radius:5px;padding:2px 7px}}
.stats{{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:auto;border-top:1px solid var(--line);padding-top:11px;color:#d7e3f2;font-size:12px}}
.empty{{grid-column:1/-1;color:var(--muted)}}footer{{color:var(--muted);border-top:1px solid var(--line);margin-top:36px;padding-top:18px;font-size:13px}}.archives{{display:flex;flex-wrap:wrap;gap:10px;margin-top:7px}}
</style></head><body><div class="wrap"><header><h1>GitHub AI Radar</h1><p>全球 AI / Agent / Coding Agent / MCP / LLM / AI 开发工具 · 基于公开仓库历史快照</p>
<div class="summary"><span>统计日期 {esc(report["date"])} UTC</span><span>候选仓库 {report["repository_count"]:,}</span><span>每周发布</span><span>不限地区或编程语言</span></div></header>
{('<div class="notice">'+" ".join(esc(n) for n in notes)+'</div>') if notes else ''}
<div class="controls"><input id="search" type="search" placeholder="搜索项目名称、简介或分类" aria-label="搜索项目"><select id="category" aria-label="筛选类别"><option value="">所有领域</option>{''.join('<option>'+esc(c)+'</option>' for c in CAT_RULES)}</select></div>
<nav class="tabs" aria-label="榜单分类">{tabs}</nav><main>{panels}</main>
<footer><p>排名说明：7/30 天新增 Star 只在存在对应日期基线快照时计算。成熟榜按累计 Star 排序，并要求最近 180 天有推送；潜力榜关注创建 90 天内且近期活跃的仓库。项目发现依赖 GitHub Search 和 Trending，不能保证覆盖全部 AI 仓库。</p>
<p>数据来自公开 GitHub 仓库；排名不代表安全审计、软件质量认证或投资建议。</p>
{('<div>历史周报：<div class="archives">'+archive_links+'</div></div>') if archive_links else ''}
{('<p><a href="../index.html">返回最新排行榜</a></p>') if archive else ''}</footer></div>
<script>
const tabs=document.querySelectorAll('[data-tab]'), panels=document.querySelectorAll('[data-panel]');
const q=document.getElementById('search'), category=document.getElementById('category');
function filter(){{const term=q.value.toLowerCase().trim(),c=category.value;document.querySelectorAll('.repo').forEach(el=>{{el.hidden=!(el.dataset.search.includes(term)&&(!c||el.dataset.categories.split('|').includes(c)));}});}}
tabs.forEach(btn=>btn.addEventListener('click',()=>{{tabs.forEach(t=>t.setAttribute('aria-selected',String(t===btn)));panels.forEach(p=>p.hidden=p.dataset.panel!==btn.dataset.tab);filter();}}));q.addEventListener('input',filter);category.addEventListener('change',filter);
</script></body></html>'''


def build() -> None:
    data = report_data()
    REPORTS.mkdir(parents=True, exist_ok=True)
    SITE.mkdir(parents=True, exist_ok=True)
    stamp = date.fromisoformat(data["date"])
    year, week, _ = stamp.isocalendar()
    filename = f"{year}-W{week:02d}"
    report_path = REPORTS / (filename + ".json")
    report_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    other_files = sorted(REPORTS.glob("????-W??.json"), reverse=True)
    archive_names = [x.stem for x in other_files[:TOPICS["report_archive_limit"]]]
    (SITE / "index.html").write_text(render_html(data, archives=archive_names), encoding="utf-8")
    (SITE / ".nojekyll").touch()
    dest = SITE / "reports"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir()
    for path in other_files[:TOPICS["report_archive_limit"]]:
        try:
            archived = json.loads(path.read_text(encoding="utf-8"))
            (dest / (path.stem + ".html")).write_text(render_html(archived, archive=True), encoding="utf-8")
        except Exception as exc:
            print(f"Could not render archive {path}: {exc}", file=sys.stderr)
    print(f"Site ready: {SITE / 'index.html'} | report={report_path.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["collect", "build"])
    args = parser.parse_args()
    if args.command == "collect":
        collect()
    else:
        build()

if __name__ == "__main__":
    main()
