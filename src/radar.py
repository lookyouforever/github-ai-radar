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
:root{{--bg:#f5f5f7;--paper:#fff;--ink:#1d1d1f;--muted:#6e6e73;--line:#d2d2d7;--blue:#06c}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}}
a{{color:var(--blue);text-decoration:none}}a:hover{{text-decoration:underline}}.wrap{{max-width:1320px;margin:auto;padding:0 28px 76px}}
.site-nav{{height:64px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid rgba(0,0,0,.08)}}.brand{{display:flex;align-items:center;gap:9px;color:var(--ink);font-size:15px;font-weight:650;letter-spacing:-.02em}}.brand-mark{{width:22px;height:22px;border-radius:50%;background:linear-gradient(135deg,#5ac8fa,#007aff 58%,#5e5ce6);box-shadow:0 2px 8px #007aff35}}.nav-links{{display:flex;align-items:center;gap:28px;font-size:13px}}.nav-links a{{color:#424245}}
.hero{{position:relative;overflow:hidden;display:grid;grid-template-columns:1.03fr .97fr;align-items:center;min-height:590px;padding:68px clamp(24px,6vw,86px) 42px;margin:22px 0 26px;border-radius:30px;background:linear-gradient(115deg,#fff 0%,#fff 44%,#eef5ff 100%);box-shadow:0 18px 60px #1d1d1f0a}}
.hero-copy{{position:relative;z-index:1;max-width:620px}}.eyebrow{{display:flex;align-items:center;gap:9px;margin:0;color:#6e6e73;font-size:12px;font-weight:650;letter-spacing:.11em;text-transform:uppercase}}.live-dot{{width:7px;height:7px;border-radius:50%;background:#34c759;box-shadow:0 0 0 4px #34c75920}}
h1{{font-size:clamp(48px,7vw,88px);line-height:.99;margin:24px 0 20px;letter-spacing:-.075em;font-weight:700}}h1 span{{color:#007aff}}.hero-lede{{max-width:470px;color:#6e6e73;font-size:19px;line-height:1.5;margin:0 0 26px}}.hero-actions{{display:flex;align-items:center;gap:20px;flex-wrap:wrap}}.primary-link{{display:inline-flex;align-items:center;gap:10px;padding:12px 18px;border-radius:24px;background:var(--blue);color:white;font-size:14px;font-weight:600;box-shadow:0 5px 15px #0066cc30}}.primary-link:hover{{background:#0077ed;text-decoration:none}}.hero-note{{color:#86868b;font-size:12px}}
.hero-art{{position:relative;display:grid;place-items:center;min-height:390px;isolation:isolate}}.hero-orb{{position:relative;width:min(29vw,350px);height:min(29vw,350px);min-width:250px;min-height:250px;border-radius:50%;background:radial-gradient(circle at 34% 28%,#fff 0 5%,#c8e7ff 13%,#7ac4ff 31%,#2388f4 54%,#0757c7 74%,#063778 100%);box-shadow:inset -22px -26px 55px #05285950,inset 12px 14px 38px #fff9,0 38px 80px #007aff35;display:grid;place-items:center}}
.hero-orb:before,.hero-orb:after{{content:"";position:absolute;inset:-13%;border:1px solid #007aff24;border-radius:50%;transform:rotate(-24deg) scaleY(.34)}}.hero-orb:after{{inset:-24%;transform:rotate(34deg) scaleY(.34);border-color:#5e5ce64a}}.orb-label{{display:grid;place-items:center;width:112px;height:112px;border-radius:32px;background:linear-gradient(145deg,#ffffffdb,#ffffff4a);border:1px solid #ffffffb3;box-shadow:0 16px 42px #043b8860;color:#fff;font-size:36px;font-weight:700;letter-spacing:-.08em;text-shadow:0 2px 12px #034eaa66;backdrop-filter:blur(14px)}}
.orbit-chip{{position:absolute;padding:9px 14px;border-radius:18px;background:#ffffffdb;border:1px solid #fff;box-shadow:0 12px 32px #18365e1a;color:#30343b;font-size:12px;font-weight:600;backdrop-filter:blur(12px)}}.chip-agent{{top:16%;left:6%}}.chip-mcp{{right:1%;top:32%}}.chip-model{{bottom:15%;left:10%}}.chip-code{{right:8%;bottom:11%}}.hero-stats{{grid-column:1/-1;display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-top:32px;padding-top:24px;border-top:1px solid #1d1d1f12}}.stat-item{{display:flex;align-items:baseline;gap:9px}}.stat-value{{font-size:19px;font-weight:650;letter-spacing:-.03em}}.stat-label{{color:var(--muted);font-size:12px}}
.notice{{padding:13px 17px;margin:18px 0 26px;background:#fff8e6;border:1px solid #f1d89a;border-radius:14px;color:#795600;font-size:13px}}
.leaderboards{{padding-top:32px;scroll-margin-top:20px}}.section-head{{display:flex;align-items:end;justify-content:space-between;gap:24px;margin:0 0 24px}}.section-head .eyebrow{{font-size:11px;color:#86868b}}h2{{margin:8px 0 0;font-size:clamp(30px,4vw,48px);line-height:1.08;letter-spacing:-.06em}}.section-copy{{max-width:370px;color:var(--muted);font-size:14px;margin:0 0 3px}}
.controls{{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0 16px}}input,select{{background:var(--paper);border:1px solid #d2d2d7;border-radius:14px;color:var(--ink);padding:13px 15px;font:inherit;box-shadow:0 2px 8px #1d1d1f08;outline:none}}
input:focus,select:focus{{border-color:#007aff;box-shadow:0 0 0 4px #007aff1a}}input{{flex:2;min-width:220px}}select{{flex:1;min-width:180px}}.tabs{{display:flex;gap:5px;flex-wrap:wrap;width:fit-content;max-width:100%;padding:5px;margin:0 0 20px;border-radius:16px;background:#e8e8ed}}.tab{{cursor:pointer;background:transparent;color:#515154;border:0;border-radius:12px;padding:10px 14px;font:inherit;font-size:13px;transition:background .18s,color .18s,box-shadow .18s}}
.tab[aria-selected=true]{{color:#1d1d1f;background:#fff;box-shadow:0 2px 8px #1d1d1f18}}.tab b{{margin-left:5px;font-size:11px;color:#86868b}}
.panel{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,320px),1fr));gap:14px}}.panel[hidden]{{display:none}}
.repo{{min-width:0;background:var(--paper);border:1px solid #e5e5ea;border-radius:20px;padding:21px;display:flex;flex-direction:column;gap:11px;box-shadow:0 5px 20px #1d1d1f08;transition:transform .18s,box-shadow .18s}}
.repo:hover{{transform:translateY(-3px);box-shadow:0 13px 32px #1d1d1f12}}.repo-top{{display:flex;gap:10px;align-items:baseline;word-break:break-word;font-weight:650}}.repo-top a{{font-size:16px;color:#06c;letter-spacing:-.02em}}.rank{{display:grid;place-items:center;min-width:25px;height:25px;border-radius:50%;background:#f2f2f7;color:#6e6e73;font-size:11px;font-weight:600;white-space:nowrap}}
.repo p{{color:#6e6e73;margin:0;min-height:44px;word-break:break-word;font-size:13px}}.tags{{display:flex;gap:6px;flex-wrap:wrap}}.tag{{font-size:10px;background:#f2f7ff;color:#2463a9;border-radius:20px;padding:4px 9px}}
.stats{{display:flex;flex-wrap:wrap;gap:7px 12px;margin-top:auto;border-top:1px solid #f0f0f3;padding-top:13px;color:#6e6e73;font-size:11px}}.stats span:first-child{{color:#9a6500;font-weight:650}}
.empty{{grid-column:1/-1;color:var(--muted);padding:45px 20px;text-align:center;background:var(--paper);border:1px dashed #d2d2d7;border-radius:20px}}footer{{color:var(--muted);border-top:1px solid var(--line);margin-top:48px;padding-top:22px;font-size:12px}}.archives{{display:flex;flex-wrap:wrap;gap:10px;margin-top:7px}}
@media(max-width:760px){{.wrap{{padding:0 16px 48px}}.site-nav{{height:56px}}.nav-links{{gap:14px}}.hero{{grid-template-columns:1fr;min-height:0;padding:44px 24px 26px;margin:14px 0 18px;border-radius:24px}}h1{{font-size:clamp(46px,12vw,68px);margin:18px 0 16px}}.hero-lede{{font-size:16px}}.hero-art{{min-height:300px;margin-top:16px}}.hero-orb{{width:245px;height:245px;min-width:0;min-height:0}}.hero-stats{{gap:8px;padding-top:18px;margin-top:14px}}.stat-item{{display:block}}.stat-value{{display:block;font-size:16px}}.stat-label{{font-size:10px}}.section-head{{display:block}}.section-copy{{margin-top:12px}}.tabs{{width:100%;display:grid;grid-template-columns:repeat(2,1fr)}}.tab{{padding:10px 8px}}.repo{{padding:18px}}
}}
@media(prefers-reduced-motion:reduce){{html{{scroll-behavior:auto}}*,*::before,*::after{{transition:none!important}}}}
</style></head><body><div class="wrap">
<div class="site-nav" id="top"><a class="brand" href="{('../index.html' if archive else '#top')}"><span class="brand-mark" aria-hidden="true"></span>AI Radar</a><nav class="nav-links" aria-label="主导航"><a href="#leaderboards">排行榜</a><a href="{('../index.html' if archive else '#top')}">最新报告</a></nav></div>
<header class="hero"><div class="hero-copy"><p class="eyebrow"><span class="live-dot" aria-hidden="true"></span>全球开源趋势 · 每日更新</p><h1>发现 AI 开源的<br><span>下一步。</span></h1><p class="hero-lede">从全球开发者社区，找到正在崛起的智能体、模型与工具。用真实的 Star 历史，看见开源创新的速度。</p><div class="hero-actions"><a class="primary-link" href="#leaderboards">浏览排行榜 <span aria-hidden="true">↓</span></a><span class="hero-note">统计截至 {esc(report["date"])} UTC</span></div></div>
<div class="hero-art" aria-hidden="true"><div class="hero-orb"><div class="orb-label">AI</div></div><span class="orbit-chip chip-agent">AI Agents</span><span class="orbit-chip chip-mcp">MCP</span><span class="orbit-chip chip-model">Open Models</span><span class="orbit-chip chip-code">Coding Tools</span></div>
<div class="hero-stats"><div class="stat-item"><strong class="stat-value">{report["repository_count"]:,}</strong><span class="stat-label">个候选仓库</span></div><div class="stat-item"><strong class="stat-value">04</strong><span class="stat-label">类社区榜单</span></div><div class="stat-item"><strong class="stat-value">每日</strong><span class="stat-label">采集 Star 快照</span></div></div></header>
{('<div class="notice">'+" ".join(esc(n) for n in notes)+'</div>') if notes else ''}
<section class="leaderboards" id="leaderboards"><div class="section-head"><div><p class="eyebrow">COMMUNITY RADAR</p><h2>本周值得关注的项目</h2></div><p class="section-copy">四种视角，追踪开源 AI 的成长：从快速上升的新星，到经受时间检验的成熟项目。</p></div>
<div class="controls"><input id="search" type="search" placeholder="搜索项目名称、简介或分类" aria-label="搜索项目"><select id="category" aria-label="筛选类别"><option value="">所有领域</option>{''.join('<option>'+esc(c)+'</option>' for c in CAT_RULES)}</select></div>
<nav class="tabs" aria-label="榜单分类">{tabs}</nav><main>{panels}</main></section>
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
