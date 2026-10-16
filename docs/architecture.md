# Architecture

```
src/claude_lens/
├── cli.py                  # Typer: import, report, cache-doctor, loops, session, insights,
│                           #        whatif, serve, dashboard
├── config.py               # Settings (env: LENS_*), is_loopback
├── models.py               # Usage, RequestRecord, ToolCall, CostBreakdown, ReportRow, Report
├── pricing.py              # pricing.yaml -> cost of one request, context window
├── data/pricing.yaml       # bundled list prices
├── fingerprint.py          # content-free hashes: prompt segments, tool inputs, file targets
├── store.py                # SQLite store (requests, tool_calls, session_meta, imported_files)
├── importers/
│   ├── records.py          # one transcript entry -> RequestRecord (no store, no logging)
│   └── claude_code.py      # Claude Code transcripts -> RequestRecord
├── proxy/
│   ├── app.py              # FastAPI pass-through for /v1/*, /healthz
│   ├── tee.py              # ResponseTee: parses a copy of the SSE / JSON response
│   ├── sessions.py         # SessionTracker: headers, metadata session id, prefix hashes (LRU)
│   └── recorder.py         # Recorder: one background thread, builds and writes records
├── analysis/
│   ├── cache.py            # cache doctor: misses, causes, penalties, advice
│   ├── loops.py            # repeated calls, error streaks, file churn
│   └── session.py          # one session: timeline, context growth, misses, loops
├── insights/
│   ├── stats.py            # per-session statistics the rules work on
│   ├── thresholds.py       # context thresholds shared with the live nudges
│   ├── rules.py            # recommendations with evidence, savings and documentation links
│   ├── practices.py        # best-practice scorecard
│   ├── whatif.py           # reprice recorded tokens on another model
│   ├── engine.py           # profile + rules + practices -> InsightsReport
│   └── coach.py            # optional LLM coach (Anthropic SDK), aggregates only
├── queries.py              # store + pricing + filters -> reports (shared by CLI and dashboard)
├── web/
│   ├── app.py              # FastAPI dashboard: page + JSON API, loopback Host only
│   └── index.html          # the dashboard (vanilla JS, inline SVG charts, no dependencies)
├── report.py               # grouping and totals
├── render.py               # rich tables
├── live/
│   ├── session.py          # incremental transcript reading, state in LENS_STATE_DIR
│   ├── statusline.py       # `statusline`: context, cost, prompt-cache state
│   ├── nudges.py           # `hook UserPromptSubmit`: one-time /clear and /compact nudges
│   └── install.py          # `install`: merge into settings.json with a backup
├── entry.py                # console script: fast path for statusline and hook
└── log_config.py           # structlog JSON on stderr
```

## Data flow

1. `importers/claude_code.py` walks `LENS_TRANSCRIPTS_DIR` (`**/*.jsonl`, subagent transcripts
   included) and turns every `assistant` record with `usage` into a `RequestRecord`. See
   [importers.md](importers.md).
2. `store.py` upserts the records by `message_id` and remembers each file's size and mtime, so the
   next import skips unchanged files.
3. `proxy/app.py` forwards each `/v1/*` request upstream and streams the response back
   unchanged. For `POST /v1/messages` a `ResponseTee` sees each chunk after the client does and
   extracts usage, tool names and timings; when the stream ends, a `RecordJob` (request body plus
   tee) goes to the `Recorder` thread, which assigns the session (`sessions.py`), builds a
   `RequestRecord` with `source="proxy"` and upserts it. See [proxy.md](proxy.md).
4. `report.py` streams records for the selected range from the store, prices each one with
   `pricing.py` and aggregates by day, project, model or session.
5. `analysis/` works on the same records and on tool calls: the cache doctor compares
   consecutive requests of a conversation, loop detection scans tool calls per session, and the
   session view combines both with the request timeline. See [analysis.md](analysis.md).
6. `insights/` builds per-session statistics (models, effort, thinking, tools, edited files,
   context peak, cache misses, loops, session metadata), runs the rules and the practice checks,
   and can hand the aggregated result to the coach. See [insights.md](insights.md).
7. `queries.py` is the one place that turns filters into those results; `cli.py` renders them
   with rich, `web/app.py` serves them as JSON to the dashboard. See [dashboard.md](dashboard.md).

## Decisions

- **One record per API response, keyed by message id.** Claude Code writes one transcript line
  per content block, all repeating the same `usage`; summing lines would count a response several
  times. Keying by message id also makes imports idempotent and deduplicates a message that shows
  up in two files.
- **Cost is computed at report time, not stored.** Changing `pricing.yaml` reprices history
  without re-importing. Pricing per request (not per aggregate) is required because of
  long-context tiers.
- **Timestamps are stored as fixed-width UTC ISO strings**, so SQLite string order equals time
  order. Day grouping uses the local time zone.
- **No prompt content in the database.** Only ids, timestamps, model, token counts, stop reason,
  tool names, and short hashes: of each prompt segment (proxy only) and of each tool input. A
  tool call also keeps the file path its input names, so file churn can be reported.
- **SQLite with `PRAGMA user_version`.** A database with a newer schema is refused rather than
  misread; an older one is migrated in place, one transaction per version (v2 added the nullable
  `ttft_ms` and `latency_ms` columns; v3 added `segments` and the `tool_calls` table, and forgets
  imported files so the next import reads tool calls from them).
- **One record per response across sources.** The upsert merges an import and a proxy record of
  the same message: the transcript's session, project and timestamp win, measured latency is
  kept, usage comes from the newest write.
- **Session metadata is stored per transcript file** and summed when read, so re-importing a
  file never double-counts prompts, interruptions or active time.
- **Insights are rules first.** Deterministic, offline and testable rules produce the findings;
  the LLM coach only rewrites them into a plan and sees aggregates, never ids, paths or prompts.
- **Passive by design.** The proxy forwards first and analyses a copy: the client gets each chunk
  before the tee parses it, and any tee or store error is logged, never raised. A single
  recorder thread owns the SQLite connection, so the request path never waits on the database
  and no lock is needed. Request bodies are parsed only there, for session grouping.
- **Forward everything, minus hop-by-hop.** Unlike claude-guard's allowlist, lens forwards all
  request headers except hop-by-hop ones and its own `x-lens-*`, so it never changes what the API
  sees.
- **Live commands bypass the CLI.** `statusline` and `hook` run on every message and prompt, so
  `entry.py` dispatches them before Typer, rich and structlog are imported, and
  `live/session.py` keeps a read offset per transcript instead of importing into SQLite. See
  [nudges.md](nudges.md).
