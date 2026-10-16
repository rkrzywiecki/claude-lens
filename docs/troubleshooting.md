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

**`database schema version N, expected 1`.** The database was written by a newer claude-lens.
Upgrade, or point `LENS_DB_PATH` at a new file and import again (the database holds only derived
data).

**Totals differ from Claude Code's own cost.** Expected within a few percent: some API calls are
not written to transcripts. See [importers.md](importers.md#known-gaps).

**A changed transcript is not re-imported.** Files are skipped when their size and mtime are
unchanged. `uv run claude-lens import --full` re-reads everything; it never duplicates records.
