# API proxy

`claude-lens serve` runs a pass-through proxy for the Anthropic Messages API. Point a client's
`ANTHROPIC_BASE_URL` at it and every request goes to the real API unchanged, while the usage of
every response is recorded in the same SQLite database the transcript importer writes to.

Unlike transcripts, the proxy sees **every** API call, including side requests Claude Code never
writes to a transcript (session titles and similar), and it measures latency.

```bash
uv run claude-lens serve                       # http://127.0.0.1:8788
export ANTHROPIC_BASE_URL=http://127.0.0.1:8788
claude
```

## Chaining with claude-guard

claude-guard inspects and may block or rewrite traffic; claude-lens only records it. Put the
guard first, so lens records what was actually sent upstream and what the API actually returned:

```
client -> claude-guard :8787 -> claude-lens :8788 -> api.anthropic.com
```

```bash
uv run claude-lens serve                                         # in claude-lens
GUARD_UPSTREAM_URL=http://127.0.0.1:8788 uv run claude-guard serve   # in claude-guard
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
claude
```

Requests the guard blocks never reach lens and are not recorded. With the guard in `buffer`
stream mode, lens still measures the API's own timing (the guard buffers after lens).

## What is forwarded

| | |
|---|---|
| Paths | Every `/v1/*` path and method (`messages`, `messages/count_tokens`, `models`, batches, files, ...), with its query string |
| Request headers | All of them, including `x-api-key`, `authorization`, `anthropic-version` and `anthropic-beta`, except hop-by-hop headers (`connection`, `transfer-encoding`, ...), `host`, `content-length`, `accept-encoding` (httpx negotiates its own) and lens's `x-lens-session` / `x-lens-project` |
| Request body | Byte for byte |
| Response | Status, headers and body unchanged, streamed as chunks arrive. Upstream errors (4xx, 5xx) are passed through verbatim. `content-length` and `content-encoding` are dropped because the body is decoded |

When the upstream cannot be reached, the client gets HTTP 502 in the Anthropic error format. When
an SSE stream breaks midway, an `error` event is appended so SDKs raise instead of waiting.

Header values are never logged.

## What is recorded

Only successful (`200`) `POST /v1/messages` responses, streamed or not, produce a record. Other
endpoints are forwarded and not recorded (`count_tokens` runs no model; batch results are not
parsed yet).

| Field | Streaming (SSE) | Non-streaming (JSON) |
|---|---|---|
| `message_id`, `model` | `message_start` | body |
| usage | `message_start`, updated by every `message_delta` (final output tokens; newer API versions also resend input and cache counts) | `usage` |
| `stop_reason` | `message_delta` | body |
| tool names | `content_block_start` of `tool_use` blocks | `tool_use` content blocks |
| tool calls | `tool_use` block id and name from `content_block_start`, input assembled from `input_json_delta` events, then hashed | `tool_use` content blocks, input hashed |
| `effort`, `features` | `output_config.effort`; streaming, `max_tokens`, tool count, whether any `cache_control` is set, thinking type, system prompt length (characters) | same |
| thinking tokens | `usage.output_tokens_details.thinking_tokens` when the API reports it | same |
| `segments` | hashes of the request's tools, system prompt, cache-relevant settings and each message | same |
| tool results | `is_error` of the `tool_result` blocks in the request's newest message, applied to the calls recorded before | same |
| `ttft_ms` | time from receiving the request to the first `content_block_start` event | none |
| `latency_ms` | time from receiving the request to the end of the stream | time to the end of the body |
| `request_id` | upstream `request-id` header | same |
| timestamp | when the proxy received the request (UTC) | same |

Times include the upstream round trip and the time the client takes to read each chunk, but not
the time the client spent before the proxy received the request. Records have `source = "proxy"`.

No prompt or response content is stored. The request body is parsed on the recorder thread to
group sessions (see below), to fingerprint the prompt and to read tool results, and then dropped.
Only short hashes remain: `segments` lets the cache doctor name the part of the prompt that
changed between two requests (see [analysis.md](analysis.md)), and a tool call keeps a hash of
its input plus the file path when its input names one (`file_path`, `notebook_path`, `path`).

A stream that breaks or a client that disconnects is still recorded with the usage seen so far:
input tokens were billed when the stream started.

## Sessions and projects

A record's session is, in order:

1. the `x-lens-session` header, if the client sends one;
2. the session id in `metadata.user_id`. Claude Code puts its own session id there (a JSON
   string with `session_id`; older versions used `user_..._session_<uuid>`), the same id its
   transcripts use, so proxy and transcript records of one session share a key;
3. message-prefix continuation: a conversation resends its whole history, so request N+1's
   `messages` start with request N's. The proxy hashes every prefix of `system` + `messages`
   (ignoring `cache_control` markers, and treating `"content": "text"` like one text block) and
   keeps the last hash of each request, with its session, in an LRU of 4096 entries. A request
   joins the session of its longest known prefix (a rewind or edit still matches the earlier
   turns) or starts a new one named `proxy-<uuid>`.

The project is the `x-lens-project` header, else the working directory Claude Code states in the
system prompt or the messages before the first assistant turn (`Primary working directory: ...`),
else the project already seen for the session, else `(proxy)`.

Prefix sessions live in memory: after a restart, a continuing conversation without a session id
in `metadata` starts a new session. Context editing that rewrites old messages (compaction,
cleared tool results) also starts a new prefix session.

Custom clients can set the headers, for example with the Python SDK:

```python
import anthropic

client = anthropic.Anthropic(
    base_url="http://127.0.0.1:8788",
    default_headers={"x-lens-session": "nightly-eval", "x-lens-project": "evals"},
)
```

## Records from both sources

When the importer and the proxy see the same response (same `message_id`), there is still one
record:

- usage, model, stop reason and tool names come from the newest write;
- `ttft_ms` and `latency_ms` are kept once the proxy measured them;
- a proxy write never replaces the session, project, timestamp or `source` of an imported
  record (the transcript knows the real session); an import replaces a proxy record's.

Reports therefore count each response once, whichever sources are running.

## Failing open

The proxy never changes the response because of recording:

- the client gets each chunk before the tee parses it; a parse error turns the tee off for that
  response and is logged as `tee_parse_failed`;
- records are built and written by a single background thread that owns the SQLite connection;
  a broken, locked or unwritable database is logged as `record_failed` and counted
  (`record_errors` in `/healthz`), and the next record tries to open it again.

## Endpoints

- `GET /healthz`: `{"status": "ok", "upstream": ..., "recorded": N, "record_errors": N}`.
- `/v1/*`: forwarded as described above.

## Listening address

The proxy speaks plain HTTP and binds to `127.0.0.1:8788` by default (`LENS_HOST`, `LENS_PORT`,
`--host`, `--port`). A non-loopback address is refused, because API keys and prompts would cross
the network unencrypted, unless `--allow-non-loopback` (or `LENS_ALLOW_NON_LOOPBACK=true`) is
given, for setups where TLS is terminated in front of the proxy.

## Settings

| Variable | Default | Description |
|---|---|---|
| `LENS_UPSTREAM_URL` | `https://api.anthropic.com` | Where requests are forwarded |
| `LENS_UPSTREAM_TIMEOUT` | `600` | Upstream timeout in seconds (connect, read, write) |
| `LENS_HOST` | `127.0.0.1` | Listen address |
| `LENS_PORT` | `8788` | Listen port |
| `LENS_ALLOW_NON_LOOPBACK` | `false` | Allow a non-loopback listen address |
| `LENS_DB_PATH` | `~/.claude-lens/lens.db` | Database the records go to |
