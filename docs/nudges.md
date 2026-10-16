# Live nudges in Claude Code

`insights` explains waste after the fact. The live commands show it while a session runs, so you
can act before the next expensive request:

- **`claude-lens statusline`**: a one-line [status line](https://code.claude.com/docs/en/statusline)
  with the context size, the session's API-equivalent cost and the prompt-cache state.
- **`claude-lens hook UserPromptSubmit`**: a [hook](https://code.claude.com/docs/en/hooks) that
  suggests `/clear` or `/compact` once a session crosses the insight thresholds.
- **`claude-lens install`**: adds both to a Claude Code `settings.json`.
- **`plugin/`**: a Claude Code plugin with the hook and a `/lens` skill that runs `insights`.

Both commands only read. They never block a prompt, never add text to Claude's context, and
never touch the database.

## Status line

```
Opus · ctx 180k/1M (18%) · $0.54 · cache warm 41m left (1h)
```

| Part | Source |
|---|---|
| `Opus` | `model.display_name` from Claude Code |
| `ctx 180k/1M (18%)` | Prompt tokens of the latest main-conversation request (input + cache reads + cache writes, as Claude Code's `used_percentage`), over the context window. Yellow from `KITCHEN_SINK_PEAK_TOKENS` (150k), red from `NEAR_LIMIT_SHARE` (60%) of the window. |
| `$0.54` | API-equivalent cost of every response in the session so far, subagents included, priced with `pricing.yaml`. A trailing `+` means some responses used a model without a price. |
| `cache warm 41m left (1h)` | Whether the cached prefix is still inside its TTL, and how long it has left. |

**Context window.** Claude Code's own `context_window.context_window_size` is used when it is in
the input (it knows whether the session runs with the 200k or the extended window); otherwise
`context_window` from `pricing.yaml`. The window seen last is remembered for the hook, which gets
no window size from Claude Code.

**Cache state.** Claude Code 2.1.251+ sends `prompt_cache` (`warm`, `ttl`, `expires_at`) and
re-runs the status line when the cache expires; that wins when present. Otherwise the state is
derived from the transcript: warm while the last main-conversation request read or wrote the
cache less than the TTL ago, where the TTL is 1 hour if the session wrote any 1-hour cache
(`cache_creation.ephemeral_1h_input_tokens`) and 5 minutes otherwise. A cold cache means the next
request writes the whole prefix again (1.25x or 2x the input price instead of 0.1x).

**Fallback.** When the transcript cannot be read, the line is built from Claude Code's
`context_window.total_input_tokens` and `cost.total_cost_usd`. Bad input prints `claude-lens`.

`install` sets `refreshInterval: 30` so the countdown keeps moving while the session idles.
`NO_COLOR` turns colors off.

## Nudge hook

Before each prompt, the hook reads the session and, when a threshold from
`insights/thresholds.py` (the same constants the insight rules use) is crossed, prints a
`systemMessage`, which Claude Code shows to you:

| Nudge | Condition | Suggestion |
|---|---|---|
| `context.near_limit` | context >= `NEAR_LIMIT_SHARE` (60%) of the window | `/compact <what to keep>`, or a short spec and a fresh session |
| `context.kitchen_sink` | >= `KITCHEN_SINK_PROMPTS` (8) prompts already in the session and context >= `KITCHEN_SINK_PEAK_TOKENS` (150k) | `/rename` then `/clear` for an unrelated task (back with `/resume`), `/compact <focus>` for the same one |

```json
{"systemMessage": "claude-lens: This session carries 180k tokens of context after 9 prompts, and every request re-reads it. New, unrelated task? `/rename` the session, then `/clear` (come back with `/resume`). Same task? `/compact <what to keep>`."}
```

Each nudge fires **at most once per session**: what fired is kept in
`LENS_STATE_DIR/nudges/<session id>.json`. `/clear` starts a new session id, so a new session
can be nudged again; `/compact` keeps the id, so a compacted session is not nudged twice. The
prompt being submitted is not counted (it is not in the transcript yet). Any error, unknown
event or missing transcript means no output and exit code 0: the hook cannot get in the way of a
prompt. Its timeout in `settings.json` is 10 seconds; a run takes about 130 ms.

## How the transcript is read

Running the status line after every message must stay well under Claude Code's 300 ms
debounce. `live/session.py` therefore reads incrementally:

- A state file per transcript (`LENS_STATE_DIR/sessions/<hash>.json`, name hashed from the path)
  keeps the byte offset reached, running totals and the last 16 responses, still open to merging
  (Claude Code writes one line per content block, all with the same message id). Older
  responses are folded into the totals.
- Each run reads only the bytes appended since; a line without its newline yet is left for the
  next run. A file that shrank, changed inode or changed its first line is read from the start.
  A different `pricing.yaml` resets the totals.
- Parsing is the importer's (`importers/records.py`), so the numbers match `report`: one record
  per message id, `<synthetic>` messages skipped. Subagent transcripts
  (`<session>/subagents/*.jsonl`) count towards the cost, not the context.
- Only counts and usage are stored, never prompt text. State files untouched for 14 days are
  removed.

`statusline` and `hook` skip the Typer CLI (`entry.py`): importing it alone takes ~280 ms.
Measured on a 14 MB transcript: ~130 ms per run end to end (Python start and imports included),
21 ms to parse the whole file the first time, 2-4 ms per incremental run.

## Install into settings.json

```bash
uv run claude-lens install            # .claude/settings.json of the current project
uv run claude-lens install --user     # ~/.claude/settings.json, every project
```

- Adds `statusLine` (`type: command`, `refreshInterval: 30`) and a `UserPromptSubmit` hook. The
  command is `uv run --quiet --project <checkout> claude-lens ...`, so it works from any project
  directory; outside a checkout it uses `claude-lens` from `PATH`.
- Other hooks, permissions and env stay as they are. Earlier claude-lens hook entries are
  replaced, not duplicated; running `install` twice changes nothing.
- Claude Code has a single status line: one that is not claude-lens's is kept, with a warning,
  unless `--force`.
- The previous file is copied to `settings.json.bak-<timestamp>`; a file that is not valid JSON
  (or has a non-object `hooks`) is never touched.
- `--no-statusline` / `--no-hook` install only one part.

The result looks like this (the path is your checkout):

```json
{
  "statusLine": {
    "type": "command",
    "command": "uv run --quiet --project /home/dev/claude-lens claude-lens statusline",
    "padding": 0,
    "refreshInterval": 30
  },
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "uv run --quiet --project /home/dev/claude-lens claude-lens hook UserPromptSubmit",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

## Plugin

`plugin/` is a Claude Code [plugin](https://code.claude.com/docs/en/plugins) named `lens`
(names starting with `claude-` are reserved for Anthropic):

```
plugin/
├── .claude-plugin/plugin.json
├── hooks/hooks.json          # UserPromptSubmit -> scripts/claude-lens hook UserPromptSubmit
├── skills/lens/SKILL.md      # /lens: runs `claude-lens insights --days 7`, Claude summarizes
└── scripts/claude-lens       # finds claude-lens: $LENS_BIN, PATH, or the checkout around plugin/
```

```bash
claude plugin validate ./plugin
claude --plugin-dir ./plugin
```

Plugins cannot set the status line (a plugin's `settings.json` only applies `agent` and
`subagentStatusLine`), so use `claude-lens install --no-hook` for it when the hook comes from the
plugin. Do not install the hook both ways, or you get each nudge twice. The `/lens` skill runs a
fixed command, pre-approved in its `allowed-tools`, with no arguments passed to the shell; it is
manual only (`disable-model-invocation`). Without claude-lens on the machine the hook stays
silent and `/lens` says how to install it.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `LENS_STATE_DIR` | `~/.claude-lens/state` | Live state: transcript read positions and nudges already shown |
| `LENS_PRICING_PATH` | *(bundled table)* | Custom `pricing.yaml` |
| `NO_COLOR` | *(unset)* | Plain status line without ANSI colors |
| `LENS_BIN` | *(unset)* | Plugin only: the claude-lens executable to run |

The live commands read the process environment only, not `.env`: they run in whatever project
Claude Code is working in. Set variables in the `env` block of `settings.json` or your shell.
