# Claude Code importer

Claude Code stores one JSONL file per session under
`~/.claude/projects/<project-dir>/<session-id>.jsonl`, and subagent transcripts under
`<session-id>/subagents/agent-<id>.jsonl`. `<project-dir>` is the working directory with `/`
replaced by `-`.

The format is not a documented, stable API. The importer relies on these fields only:

| Field | Use |
|---|---|
| `type == "assistant"` | Only these records represent API responses |
| `message.id` | Record key (merging and deduplication) |
| `message.model` | Pricing; `<synthetic>` (no API call) is skipped |
| `message.usage` | Token counts, `cache_creation` TTL split, `speed` |
| `message.content[].type == "tool_use"` | Tool names |
| `message.stop_reason` | Stored as is |
| `sessionId`, `timestamp`, `cwd`, `isSidechain`, `requestId` | Grouping and metadata |
| `effort`, `agentId` on assistant records | Effort per response, subagent id |
| `message.usage.output_tokens_details.thinking_tokens` | Thinking tokens |
| `entrypoint`, `version` | Session metadata: interface (cli, claude-desktop, sdk-ts, ...) and version |
| user records: `permissionMode`, `origin.kind`, `interruptedMessageId`, `toolDenialKind` | Permission modes used, prompts typed by a person, interruptions, denied tool calls |
| assistant records: `isApiErrorMessage`, `attributionSkill`, `attributionAgent` | API errors, skills and subagent types used |
| system records with `subtype: turn_duration` (`durationMs`) | Active time |

Rules:

- One response is logged as several records (one per content block) with the same `message.id`.
  They are merged: tool names from every block, usage from the record with the most output tokens,
  and the earliest timestamp.
- `cache_creation_input_tokens` without a TTL split count as 5-minute writes. When the split does
  not add up to the total, the difference is counted as 5-minute writes.
- Non-integer or negative token counts become 0.
- When a record has no `cwd`, the project is the top-level directory name under the transcript
  root (for example `-home-dev-shop`).
- Malformed lines are counted and skipped; unknown record types are ignored.
- Subagent records carry the parent's `sessionId`, so they are reported with the parent session.

A **prompt typed by a person** is a user record whose `origin.kind` is `human`; older transcripts
without `origin` count user records that are not meta context and carry no tool result.
Session metadata is stored per transcript file and summed when read.

## Known gaps

Claude Code also makes API calls that are not written to the transcript, such as session-title
generation on a small model. Each session's `cost-state` record holds Claude Code's own total; on
real data, claude-lens totals came out about 4% lower, and many sessions matched to the cent.
Run the [proxy](proxy.md) to record those calls too. A response seen by both the importer and the
proxy is stored once (keyed by message id); the importer's session and project win, and the
proxy's latency is kept.
