# Architecture

```
src/claude_lens/
├── cli.py                  # Typer: import, report, serve
├── config.py               # Settings (env: LENS_*), is_loopback
├── models.py               # Usage, RequestRecord, CostBreakdown, ReportRow, Report (pydantic)
├── pricing.py              # pricing.yaml -> cost of one request
├── data/pricing.yaml       # bundled list prices
├── store.py                # SQLite store (requests, imported_files)
├── importers/
│   └── claude_code.py      # Claude Code transcripts -> RequestRecord
├── proxy/
│   ├── app.py              # FastAPI pass-through for /v1/*, /healthz
│   ├── tee.py              # ResponseTee: parses a copy of the SSE / JSON response
│   ├── sessions.py         # SessionTracker: headers, metadata session id, prefix hashes (LRU)
│   └── recorder.py         # Recorder: one background thread, builds and writes records
├── report.py               # grouping and totals
├── render.py               # rich tables
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
- **No prompt content in the database.** Only ids, timestamps, model, token counts, stop reason and
  tool names.
- **SQLite with `PRAGMA user_version`.** A database with a newer schema is refused rather than
  misread; an older one is migrated in place, one transaction per version (v2 added the nullable
  `ttft_ms` and `latency_ms` columns).
- **One record per response across sources.** The upsert merges an import and a proxy record of
  the same message: the transcript's session, project and timestamp win, measured latency is
  kept, usage comes from the newest write.
- **Passive by design.** The proxy forwards first and analyses a copy: the client gets each chunk
  before the tee parses it, and any tee or store error is logged, never raised. A single
  recorder thread owns the SQLite connection, so the request path never waits on the database
  and no lock is needed. Request bodies are parsed only there, for session grouping.
- **Forward everything, minus hop-by-hop.** Unlike claude-guard's allowlist, lens forwards all
  request headers except hop-by-hop ones and its own `x-lens-*`, so it never changes what the API
  sees.
