# Troubleshooting

**`No requests in the selected range`.** Check that `LENS_TRANSCRIPTS_DIR` points at the
transcripts (`ls ~/.claude/projects`) and run `uv run claude-lens import`. With `--days`, `--since`
or `--until`, days are local calendar days.

**`Transcript directory not found`.** `import` needs an existing directory; pass `--dir` or set
`LENS_TRANSCRIPTS_DIR`. `report` silently skips the refresh when the directory is missing.

**`No price for <model>`.** The model is not in the price table. Copy
`src/claude_lens/data/pricing.yaml`, add the model, and set `LENS_PRICING_PATH`.

**`cannot load pricing`.** The custom table is unreadable, not valid YAML, or has a missing or
unknown key. The message names the field.

**`database schema version N, expected 2`.** The database was written by a newer claude-lens.
Upgrade, or point `LENS_DB_PATH` at a new file and import again (the database holds only derived
data). Older databases are upgraded in place when opened (version 1 gains the latency columns).

**Totals differ from Claude Code's own cost.** Expected within a few percent: some API calls are
not written to transcripts. See [importers.md](importers.md#known-gaps).

**A changed transcript is not re-imported.** Files are skipped when their size and mtime are
unchanged. `uv run claude-lens import --full` re-reads everything; it never duplicates records.

**`Refusing to listen on non-loopback address`.** `serve` binds to `127.0.0.1` unless
`--allow-non-loopback` (or `LENS_ALLOW_NON_LOOPBACK=true`) is given. Only use it with TLS
terminated in front of the proxy.

**`address already in use` from `serve`.** Another process holds port 8788 (perhaps an earlier
proxy). Stop it or pass `--port`.

**The proxy records nothing.** Check `curl http://127.0.0.1:8788/healthz`: `recorded` grows with
every successful `POST /v1/messages`. If `record_errors` grows instead, the database cannot be
written; the stderr log has `record_failed` with the reason (raise `LENS_LOG_LEVEL` to `INFO` to
also see uvicorn's request lines). Requests that a claude-guard in front blocks never reach lens.

**Proxy requests land in `(proxy)` or in many one-request sessions.** The client sent no session
id in `metadata` and no working directory; set `x-lens-session` and `x-lens-project` headers. See
[proxy.md](proxy.md#sessions-and-projects).

**HTTP 502 `claude-lens could not reach the upstream API`.** `LENS_UPSTREAM_URL` is wrong or
unreachable, or the connection timed out (`LENS_UPSTREAM_TIMEOUT`).
