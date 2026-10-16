# Changelog

All notable changes to claude-lens. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-16

First release. Everything runs locally; prompt contents are never stored.

### Added

- **Transcript importer and reports.** `import` reads Claude Code transcripts incrementally into
  SQLite, one record per API response (merged by message id, subagents included); `report`
  shows API-equivalent cost, tokens, prompt-cache hit ratio and savings by day, project, model,
  effort or session, as a table or JSON. Prices and context windows live in `pricing.yaml`.
- **API proxy.** `serve` forwards `/v1/*` to the Anthropic API unchanged and records every
  Messages response (usage, latency, time to first token, tool calls, prompt-segment hashes),
  failing open; it chains behind claude-guard.
- **Cache doctor, loops and sessions.** `cache-doctor` finds prompt-cache misses, names the cause
  (TTL expired, prefix changed, unexplained, context reset) and prices the avoidable part;
  `loops` flags repeated calls, error streaks and file churn; `session` shows one session's
  timeline, context growth, misses and loops.
- **Dashboard.** `dashboard` serves a local page and JSON API: totals, recommendations, cost per
  day, models and projects, cache doctor, loops, trends, outcomes and a session view. Loopback
  only, no external assets, POST actions behind an action header.
- **Insights.** `insights` turns the activity into recommendations with evidence, documentation
  links and priced savings (model choice, effort, context hygiene, subagents, plan mode, rework,
  permissions, CLAUDE.md, prompt cache, API applications) plus a best-practice scorecard.
  `--coach` adds a plan written by Claude from aggregates only, through the API or, without an
  API key, through the signed-in Claude Code CLI. `whatif` reprices traffic on another model.
- **Trends.** `trends` measures the habits behind the recommendations week by week;
  `insights --compare` compares a range with the one before it.
- **Outcomes.** Commits, files and lines per session from git (counts only); cost per commit in
  `report --outcomes`, `session`, `insights` and the dashboard.
- **Live nudges.** `statusline` for Claude Code (context, cost, cache state), a
  `UserPromptSubmit` hook that suggests `/clear` or `/compact` once per session, `install` to
  merge both into settings with a backup, and the `lens` Claude Code plugin with a `/lens` skill.
- **Tooling.** CI on Python 3.12 and 3.13 (ruff, mypy strict, pytest with coverage); the test
  suite fails on any warning and never uses the network, a Claude login or an API key.

[Unreleased]: https://github.com/rkrzywiecki/claude-lens/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rkrzywiecki/claude-lens/releases/tag/v0.1.0
