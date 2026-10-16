# Tutorial: from the first report to better habits

This walkthrough takes about 20 minutes. Parts 1-3 use the sample data in `examples/`, so they
work before you have any history of your own. From part 4 on you use your own Claude Code
sessions.

| Part | You will |
|---|---|
| [0. Install](#0-install) | get `claude-lens` running |
| [1. First report](#1-first-report-on-the-sample-data) | see what two sessions cost and why one cost three times more |
| [2. Find the cause](#2-find-the-cause) | read the cache doctor and a session timeline |
| [3. Decide what to change](#3-decide-what-to-change) | get recommendations, a what-if price, and a coach's plan |
| [4. Your own data](#4-switch-to-your-own-data) | run the same on your sessions, in the terminal and the dashboard |
| [5. Live nudges](#5-get-nudged-while-you-work) | see context and cost inside Claude Code while you work |
| [6. Track progress](#6-check-that-it-helped) | check week by week whether the changes helped |
| [7. API applications](#7-optional-your-own-api-applications) | record your own apps through the proxy (optional) |
| [8. Configure](#8-configure) | prices, coach language, where data lives |
| [Recipes](#recipes) | answer everyday questions with one command |

All numbers are **API-equivalent list prices**: on a Claude subscription you do not pay per
token, and the figures show what the same usage would cost on the API.

## 0. Install

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/). No API key is needed.

```bash
git clone https://github.com/rkrzywiecki/claude-lens.git
cd claude-lens
uv sync
uv tool install .
claude-lens --help
```

`uv tool install .` puts `claude-lens` on your `PATH`, so it works from any directory and the
Claude Code plugin can find it. Inside the checkout, `uv run claude-lens ...` works too.

## 1. First report on the sample data

Point claude-lens at the bundled transcripts and a throwaway database:

```bash
export LENS_TRANSCRIPTS_DIR=examples/transcripts LENS_DB_PATH=examples/demo.db
claude-lens report
```

```
API-equivalent cost by day
┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━┳━━━━━━━┓
┃ Day        ┃ Requests ┃ Input ┃ Cache write ┃ Cache read ┃ Output ┃ Hit ratio ┃ Saved ┃  Cost ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━╇━━━━━━━┩
│ 2026-10-14 │       12 │    36 │      159.0k │     150.0k │   9.6k │       49% │ $0.49 │ $0.59 │
│ 2026-10-15 │       12 │    36 │      231.0k │     222.0k │   9.6k │       49% │ $0.72 │ $0.79 │
├────────────┼──────────┼───────┼─────────────┼────────────┼────────┼───────────┼───────┼───────┤
│ Total      │       24 │    72 │      390.0k │     372.0k │  19.2k │       49% │ $1.21 │ $1.38 │
└────────────┴──────────┴───────┴─────────────┴────────────┴────────┴───────────┴───────┴───────┘
```

`report` imported the transcripts on the way (every analysis command imports what changed
first). The columns: **Input** is uncached input, **Cache write** and **Cache read** are prompt-
cache traffic, **Hit ratio** is the share of prompt tokens served from the cache, **Saved** is
what caching saved compared with no caching.

Now group by session:

```bash
claude-lens report --by session
```

```
API-equivalent cost by session
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━┓
┃ Session                          ┃ Requests ┃ Input ┃ Cache write ┃ Cache read ┃ Output ┃ Hit ratio ┃  Saved ┃  Cost ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━┩
│ 7b2e4d10  2026-10-14 11:00       │       12 │    36 │      372.0k │          0 │   9.6k │        0% │ -$0.19 │ $1.03 │
│ /home/dev/blog                   │          │       │             │            │        │           │        │       │
│ 1f0c9a52  2026-10-14 11:00       │       12 │    36 │       18.0k │     372.0k │   9.6k │       95% │  $1.40 │ $0.36 │
│ /home/dev/shop                   │          │       │             │            │        │           │        │       │
├──────────────────────────────────┼──────────┼───────┼─────────────┼────────────┼────────┼───────────┼────────┼───────┤
│ Total                            │       24 │    72 │      390.0k │     372.0k │  19.2k │       49% │  $1.21 │ $1.38 │
└──────────────────────────────────┴──────────┴───────┴─────────────┴────────────┴────────┴───────────┴────────┴───────┘
```

Both sessions did the same amount of work. The first read **nothing** from the cache and shows
**negative** savings: it paid the cache-write premium (1.25x input) on every request and never
read the result back. That is the kind of waste the next parts find for you.

Other views: `--by model`, `--by project`, `--by effort`, `--by day` (default), plus `--days N`,
`--since`/`--until` and `--project TEXT` filters, and `--json` for scripts.

## 2. Find the cause

The **cache doctor** explains cache misses:

```bash
claude-lens cache-doctor
```

```
Prompt-cache misses by session
┏━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┓
┃ Session  ┃ Started          ┃ Project        ┃ Requests ┃ Hit ratio ┃ Misses                     ┃  Lost ┃ Avoidable ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━┩
│ 7b2e4d10 │ 2026-10-14 11:00 │ /home/dev/blog │       12 │        0% │ 1 TTL expired, 10          │ $0.76 │     $0.69 │
│          │                  │                │          │           │ unexplained                │       │           │
│ 1f0c9a52 │ 2026-10-14 11:00 │ /home/dev/shop │       12 │       95% │ -                          │ $0.00 │     $0.00 │
└──────────┴──────────────────┴────────────────┴──────────┴───────────┴────────────────────────────┴───────┴───────────┘
Listed sessions: $0.76 lost to cache misses, $0.69 avoidable.
...
Advice:
  - 10 misses within the TTL have no visible cause in transcripts. Route the client through `claude-lens serve` to see
which segment changed.
```

- **TTL expired**: a pause longer than the cache lifetime (5 minutes, or 1 hour).
- **Unexplained**: within the TTL, the prompt prefix changed. From transcripts claude-lens cannot
  see the prompt itself; through the proxy (part 7) it names the part that changed.
- **Avoidable** is the cost a stable prefix or a longer TTL would have saved.

Open the session (a unique prefix of the id is enough):

```bash
claude-lens session 7b2e
```

```
Session 7b2e4d10-busted  /home/dev/blog
2026-10-14 11:00 - 2026-10-15 11:44, 12 requests, $1.03, cache hit ratio 0%
Context: peak 42.0k tokens (4% of the window), last 42.0k, +2,000 tokens per request, 0 resets
┃ Time     ┃ Model           ┃ Prompt ┃ Cache read ┃ Cache write ┃ Output ┃  Cost ┃ Tools / notes            ┃
│ 11:00:00 │ claude-sonnet-… │  20.0k │          0 │       20.0k │    800 │ $0.06 │ Bash                     │
│ 11:04:00 │ claude-sonnet-… │  22.0k │          0 │       22.0k │    800 │ $0.06 │ cache miss: unexplained  │
...
```

`claude-lens loops` lists agent loops: the same call repeated, error streaks, and files edited
over and over (this sample session rewrote `app.py` five times).

## 3. Decide what to change

**Insights** turn everything into a ranked list of recommendations, each with evidence, a link
to the documentation it comes from and, where possible, a priced saving:

```bash
claude-lens insights --no-check-files
```

```
1. HIGH Avoidable prompt-cache misses  repriced $0.69
   ...
2. HIGH Opus or Fable for sessions that changed no files  repriced $0.14
   1 session ran the main conversation on the largest models but only read code, answered
   questions or ran commands (no edits): $0.36.
   → For questions, reviews and exploration, try Sonnet (`/model sonnet`) or the `opusplan`
   alias, and keep Opus for complex architecture and multi-step reasoning. ...
3. MEDIUM Rework: repeated edits, error streaks, interruptions
...
Best practices
│ ✖ fail │ Match the model to the task          │ 1 of 1 sessions without edits ran on Opus or Fable │
│ ✖ fail │ Verify work and course-correct early │ 1 of 1 sessions with edits show rework ...         │
...
```

(`--no-check-files` skips the CLAUDE.md check, because the sample projects are not on disk.)

Savings are labeled: **repriced** means the same tokens priced differently, **estimate** rests on a
stated assumption, **up to** is an upper bound. The logs show cost, not quality, so a cheaper
model or lower effort is an experiment to try on real tasks.

Price such an experiment before you run it:

```bash
claude-lens whatif --to claude-sonnet-5-5 --from opus
```

```
12 requests (model ~ opus, all): $0.36 as recorded, $0.22 on claude-sonnet-5-5: $0.14 difference.
```

Finally, ask the **coach** for a short plan in your language. See what it would receive first,
aggregates only, no paths, session ids or prompts:

```bash
claude-lens insights --coach --dry-run --no-check-files | jq -r '.findings[].title'
claude-lens insights --coach --lang Polish --no-check-files
```

The coach uses the Claude API when `ANTHROPIC_API_KEY` is set (a few cents per report), and
otherwise your signed-in Claude Code (`claude -p`), which counts against your plan instead. It
writes the three most important changes, a model and effort plan, what you already do well, and
what to measure next time.

## 4. Switch to your own data

Drop the sample settings; claude-lens now reads `~/.claude/projects` and keeps its database in
`~/.claude-lens/lens.db`:

```bash
unset LENS_TRANSCRIPTS_DIR LENS_DB_PATH
claude-lens import
claude-lens report --by project --days 7
claude-lens insights --days 30
```

The first import reads every transcript (seconds for hundreds of sessions); later runs read only
what changed. Then open the dashboard:

```bash
claude-lens dashboard
```

Open `http://127.0.0.1:8789`. One row of filters (7/30/90 days, project) drives everything: totals,
**Recommendations** with the best-practice scorecard and **Ask Claude for a plan**, cost per day,
models, projects, the cache doctor, loops, trends and outcomes. Click a session id anywhere to
see its timeline with every cache miss marked. The page works offline and only on your machine.

## 5. Get nudged while you work

Reports tell you afterwards. Live nudges show the numbers inside Claude Code while you work:

```bash
claude-lens install --user
```

This adds two things to `~/.claude/settings.json` (a backup is written next to it first):

- a **status line** with the session's context, cost and cache state:

  ```
  Opus · ctx 180k/1M (18%) · $0.54 · cache cold (1h)
  ```

- a **hook** that, once per session, suggests a reset when the context has grown large over many
  prompts. It never blocks your prompt:

  ```
  claude-lens: This session carries 180k tokens of context after 9 prompts, and every request
  re-reads it. New, unrelated task? `/rename` the session, then `/clear` (come back with
  `/resume`). Same task? `/compact <what to keep>`.
  ```

Try both without Claude Code on the sample session:

```bash
echo '{"session_id": "c0ffee00-0000-4000-8000-000000000001", "transcript_path": "examples/nudges/c0ffee00-long.jsonl", "model": {"display_name": "Opus"}}' | claude-lens statusline
```

Prefer a plugin? The repository includes one with the same hook and a `/lens` command that
summarizes your last 7 days:

```bash
claude --plugin-dir ./plugin
```

Use `install` without `--user` to enable it for one project only. To remove it, restore the
`settings.json.bak-<timestamp>` file or delete the claude-lens entries (`statusLine` and the
`UserPromptSubmit` hook). Details: [nudges.md](nudges.md).

## 6. Check that it helped

After a week or two, compare:

```bash
claude-lens trends --weeks 8
claude-lens insights --days 14 --compare
```

`trends` measures the habits behind the recommendations week by week: context cost, long
sessions without a reset, plan mode for large changes, rework, cache hit ratio, model and effort
mix. Arrows are green for an improvement and red for a regression. On the sample data in
`examples/trends`:

```
┃ Metric                      ┃ 26-W39 ┃  26-W40 ┃  26-W41 ┃ 26-W42* ┃   Change ┃
│ API-equivalent cost         │  $1.04 │ $1.04 = │ $0.56 ↓ │ $0.46 ↓ │ -$0.10 ↓ │
│ Context cost                │  $0.14 │ $0.14 = │ $0.06 ↓ │ $0.06 = │        = │
│ Long sessions without reset │   100% │  100% = │    0% ↓ │    0% = │        = │
│ Large changes planned       │     0% │  100% ↑ │  100% = │  100% = │        = │
```

Cost alone does not say whether work got done. For sessions in a git repository, **outcomes**
add the commits made during the session:

```bash
claude-lens report --outcomes --by model
```

```
Cost and git outcome by main model
┏━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Model             ┃ Sessions ┃ Commits ┃ Files ┃ +/- lines ┃ Edits, no commit ┃  Cost ┃ Per commit ┃ Per 100 lines ┃
┡━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ claude-opus-5-5   │        2 │       2 │     2 │ +120 / -0 │                1 │ $5.17 │      $2.59 │         $4.31 │
│ claude-sonnet-5-5 │        1 │       1 │     1 │  +25 / -0 │                0 │ $0.16 │      $0.16 │         $0.64 │
├───────────────────┼──────────┼─────────┼───────┼───────────┼──────────────────┼───────┼────────────┼───────────────┤
│ Total             │        3 │       3 │     3 │ +145 / -0 │                1 │ $5.34 │      $1.78 │         $3.68 │
└───────────────────┴──────────┴─────────┴───────┴───────────┴──────────────────┴───────┴────────────┴───────────────┘
```

A commit is a rough sign of kept work, not of quality, and model comparisons are correlations.

## 7. (Optional) Your own API applications

Transcripts cover Claude Code. For your own applications built on the Claude API (or to see
every Claude Code request, including side calls), run the proxy and point the client at it:

```bash
claude-lens serve
export ANTHROPIC_BASE_URL=http://127.0.0.1:8788
```

The proxy forwards everything unchanged and records usage, latency and, for the cache doctor,
hashes of each prompt part, so misses on proxy traffic name what changed (tool definitions,
system prompt, settings, or an earlier message). Insights then add API-specific advice: prompt
caching for large repeated prompts, short answers from big models, streaming for large
`max_tokens`, batch candidates. With [claude-guard](https://github.com/rkrzywiecki/claude-guard),
put the guard first. Details: [proxy.md](proxy.md).

## 8. Configure

Settings are environment variables (or a `.env` file in the directory you run from; start from
`.env.example`). The ones you are most likely to change:

| Variable | Use |
|---|---|
| `LENS_DB_PATH` | Keep the database elsewhere (default `~/.claude-lens/lens.db`) |
| `LENS_TRANSCRIPTS_DIR` | Read transcripts from another directory |
| `LENS_PRICING_PATH` | Your own copy of `pricing.yaml` (contracted rates, new models) |
| `LENS_COACH_LANGUAGE` | Default coach language, e.g. `Polish` |
| `LENS_COACH_MODEL`, `LENS_COACH_EFFORT` | Which model writes the plan, at which effort |
| `LENS_COACH_BACKEND` | `auto`, `api` or `claude-code` |

All variables: [README](../README.md#configuration). Prices: [pricing.md](pricing.md).

## Recipes

| Question | Command |
|---|---|
| What did the last 7 days cost, per project? | `claude-lens report --by project --days 7` |
| Which 5 sessions cost the most this month? | `claude-lens report --by session --days 30 --top 5` |
| Where does my effort setting spend? | `claude-lens report --by effort --days 30` |
| Why was this session expensive? | `claude-lens session <id prefix>` |
| Am I losing money on the prompt cache? | `claude-lens cache-doctor --days 30` |
| Is the agent going in circles? | `claude-lens loops --days 30` |
| Would Sonnet be enough for my main conversations? | `claude-lens whatif --to claude-sonnet-5-5 --from opus --part main` |
| Are my subagents on the right model? | `claude-lens whatif --to claude-haiku-5-5 --part subagents` |
| What should I change, as a plan? | `claude-lens insights --days 30 --coach --lang Polish` |
| Did last week's changes help? | `claude-lens insights --days 7 --compare` |
| Cost per day as CSV for a spreadsheet | `claude-lens report --json \| jq -r '.rows[] \| [.key, .cost.total] \| @csv'` |

Something not working? See [troubleshooting.md](troubleshooting.md).
