# Analysis: cache doctor, loops, sessions

All three work on what is already in the database; none of them calls an API.

```bash
uv run claude-lens cache-doctor [--days N] [--project TEXT] [--top 10] [--json]
uv run claude-lens loops [--days N] [--project TEXT] [--json]
uv run claude-lens session <id or unique prefix> [--json]
```

Each command imports changed transcripts first (`--no-refresh` skips that).

## Cache doctor

Within one conversation, every request normally reads from the prompt cache what the previous
request read or wrote, plus a little more. The cache doctor compares consecutive requests of a
**stream** (same session, same model, main conversation only: caches are per model, and subagents
interleave with their own prefixes) and reports a **miss** when a request reads markedly less
than the previous one cached: at least 1,024 tokens and at least 20% of the previous cached
prefix.

| Cause | When | Avoidable |
|---|---|---|
| `context_reset` | The prompt shrank by more than half: compaction, `/clear`, or a side call sharing the session | No, and no penalty |
| `prefix_changed` | Proxy records only: the first differing prompt segment is known (tool definitions, system prompt, thinking/effort/tool_choice, or message N of the history) and the gap was within the TTL | Yes |
| `ttl_expired` | More time passed than the cache TTL: 5 minutes, or 1 hour once the stream writes with the 1-hour TTL | Only for a 5-minute entry and a pause of up to an hour (a 1-hour TTL or a keep-alive would have kept it) |
| `unexplained` | Within the TTL and no visible change. From transcripts the prompt is unknown, so most prefix changes land here; with the proxy it means the entry was evicted | Yes |

**Penalty** of a miss: the tokens that had to be sent again (at most what the request actually
wrote to the cache or sent uncached), priced at the write price (1.25x or 2x input, by the
request's TTL split) or the input price, minus what reading them from the cache would have cost.
Sessions are listed by avoidable penalty, then total penalty.

**Advice** is derived from the misses: pauses of 5-60 minutes suggest the 1-hour TTL or a
keep-alive, the most frequent changed segment is named, unexplained misses suggest running
through the proxy, and large requests with no cache reads or writes at all suggest adding
`cache_control` breakpoints.

### Prompt segments

The proxy hashes each request's prompt in cache order, each part on its own (`fingerprint.py`):

```
[tools, system, params (thinking, effort, tool_choice), message 1, message 2, ...]
```

`cache_control` markers are removed before hashing (clients move them every turn), and
`"content": "text"` hashes like a single text block. Comparing two requests position by position
gives the first segment that changed; everything after it had to be written again.

## Loops

Tool calls come from transcripts (`tool_use` blocks, with `is_error` from the matching
`tool_result`) and from the proxy (response `tool_use` blocks; results from the next request).
Only a hash of each input is stored, plus the file path for file tools.

| Pattern | Rule |
|---|---|
| `repeated_call` | The same tool with the same input 3 or more times in a session. Routine tools whose input repeats by design (`*update_plan`, `TodoWrite`, `TaskUpdate`, `TaskList`) are skipped |
| `error_streak` | 3 or more consecutive tool results that were errors. A call whose result is unknown neither extends nor breaks a streak |
| `file_churn` | The same file changed by `Edit`, `MultiEdit`, `Write`, `NotebookEdit` or `str_replace_based_edit_tool` 5 or more times in a session |

These are signals, not verdicts: re-running the same test after each fix is a repeated call too.

## Session view

`claude-lens session <id>` accepts a full session id or a unique prefix (`session 89f3`). It shows:

- totals: time span, requests, cost, cache hit ratio;
- context: peak prompt tokens and the share of the model's context window (`context_window` in
  `pricing.yaml`), last prompt size, average growth per request of the main conversation, number of
  resets;
- a timeline with one row per request: prompt, cache read and write, output, cost, tools, and the
  cache-miss cause where there was one; subagent requests are marked;
- the loops of that session.

With `--json` the same data is printed as JSON (`SessionDetail`).
