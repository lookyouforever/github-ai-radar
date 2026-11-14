# GitHub AI Radar

自动发现全球 AI / Agent / Coding Agent / MCP / LLM / CLI 项目，每天采集公开 GitHub 仓库 Star 快照，每周发布四大排行榜至 GitHub Pages。

## 一键部署（推荐）

在已经安装 Git、Python 3、GitHub CLI 且 `gh auth login` 已登录 `lookyouforever` 的电脑上，解压项目，进入目录后执行：

```bash
bash deploy.sh
```

此脚本会在当前 GitHub 账号下创建公开仓库，推送代码，设置 Actions 权限与 Pages，运行首次采集和部署，并验证工作流结果。脚本不会修改已存在的同名仓库。GitHub CLI 使用你设备上的现有授权，不需要向任何人发送 Token。

## 手动上线步骤（备选）

1. 创建 **Public** 仓库 `github-ai-radar`，默认分支使用 `main`。
2. 将本项目的所有文件上传至仓库根目录（保留 `.github/workflows/` 目录）。
3. 进入 **Settings → Actions → General → Workflow permissions**，选择 **Read and write permissions** 并保存。如组织策略禁止，请让仓库管理员调整或改用可写部署方式。
4. 进入 **Settings → Pages → Build and deployment → Source**，选择 **GitHub Actions**。
5. 进入 **Actions → Daily repository snapshots → Run workflow**，先手动运行一次采集。确认成功提交 `data/snapshots/YYYY-MM-DD.json.gz`。
6. 进入 **Actions → Weekly leaderboard and Pages → Run workflow**，首次构建并发布网站。预期地址：`https://<username>.github.io/github-ai-radar/`。
7. 以后每日 **02:17 UTC** 采集，每周日 **03:17 UTC** 发布。GitHub 定时任务可能延迟或漏跑，必要时手动补采。

> 注意：首次部署时没有 7 天和 30 天历史基线。这两个榜单会提示“数据积累中”；不要误以为 Star 增长为 0。

## 文件结构

- `src/radar.py`：仅使用 Python 标准库，包含 GitHub Search/Trending 项目发现、采集、历史计算及网页生成。
- `config/topics.json`：主题、Star 阈值、榜单大小等可调整配置。
- `.github/workflows/collect.yml`：每日采集并提交压缩历史快照。
- `.github/workflows/report.yml`：每周生成排行榜、保存周报、部署 Pages。
- `data/snapshots/`：最近 45 天每日压缩 JSON 快照；Git 历史中仍会保留旧版本。
- `reports/`：每周生成的 JSON 历史报告。
- `site/`：构建时生成的静态网站，默认不提交 Git。

## 排名定义

- **7 天新锐榜**：必须存在恰好 7 天前及当前日期的仓库快照，按照新增 Star 降序。
- **30 天新锐榜**：必须存在恰好 30 天前及当前日期的仓库快照，按照新增 Star 降序。
- **成熟榜**：总 Star >= 1,000，最近 180 天有代码推送，按总 Star 排序。
- **潜力榜**：创建不超过 90 天、至少 20 Star、最近 180 天有代码推送，优先按可计算的 7 天 Star 增量排序，否则按总 Star 排序。

## 数据完整性说明

GitHub Search 每个查询最多返回 1,000 条结果，GitHub Trending 不提供官方独立 API，HTML 解析可能因页面更新失效。此项目只跟踪搜索命中与 Trending 发现的候选仓库，并不等于穷尽全 GitHub。没有对应日期历史数据的项目显示“待积累”，不会捏造增长数据。每日采集部分失败会写入快照并在报告中提示。

## 安全和成本

仅使用 Actions 提供的 `GITHUB_TOKEN`，不要把个人 Token 写入仓库。GitHub 的 API 与 Actions 配额限制取决于账户及仓库策略。默认不使用付费第三方服务。

## 开发及测试

```bash
python -m unittest discover -s tests -v
python src/radar.py build  # 需先准备 data/snapshots/ 中的真实快照
```
