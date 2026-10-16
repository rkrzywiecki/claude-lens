# Architecture

```
src/claude_lens/
├── cli.py                  # Typer: import, report
├── config.py               # Settings (env: LENS_*)
├── models.py               # Usage, RequestRecord, CostBreakdown, ReportRow, Report (pydantic)
├── pricing.py              # pricing.yaml -> cost of one request
├── data/pricing.yaml       # bundled list prices
├── store.py                # SQLite store (requests, imported_files)
├── importers/
│   └── claude_code.py      # Claude Code transcripts -> RequestRecord
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
3. `report.py` streams records for the selected range from the store, prices each one with
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
  misread.
- **Passive by design.** claude-lens never sits in the request path in M1. The planned proxy (M2)
  will forward first and analyse a copy of the stream, failing open.
