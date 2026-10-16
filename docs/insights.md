# Insights

```bash
uv run claude-lens insights [--days N] [--project TEXT] [--json] [--no-check-files]
uv run claude-lens insights --coach [--lang Polish]      # add a plan written by Claude (paid)
uv run claude-lens insights --coach --dry-run            # show what the coach would receive
uv run claude-lens insights --days 14 --compare          # add "since last period"
uv run claude-lens whatif --to claude-sonnet-5-5 [--from opus] [--part all|main|subagents]
```

`insights` turns the recorded activity into recommendations: which model and effort level to
use, how to keep context small, how to keep the prompt cache warm, which workflow habits cost
rework, and which documented best practices the activity follows. Every recommendation has the
numbers it rests on, the sessions it applies to, a link to the documentation it comes from and,
where it can be priced, an estimated saving.

It complements Claude Code's own `/insights`, which has Claude read your sessions and describe
how you work. claude-lens works offline from token counts and tool calls, puts a dollar figure
on each finding, combines it with cache-doctor and loop data, and also covers your own API
applications through the proxy. The coach is optional.

## What it reads

| Data | From | Used for |
|---|---|---|
| Tokens, cost, model, cache | Transcripts and proxy | Everything |
| `effort` per response | Transcripts (`effort`), proxy (`output_config.effort`) | Effort rules, profile |
| Thinking tokens | `usage.output_tokens_details.thinking_tokens` | Effort rules, profile |
| Subagent requests | Transcripts (`isSidechain`, `agentId`) | Subagent model rules |
| Tool calls, edited files | Transcripts and proxy | Light sessions, exploration, plan mode, loops |
| Session metadata | Transcripts: interface, version, permission modes, prompts typed, interruptions, tool denials, API errors, skills, active time | Context, workflow and permission rules |
| Request settings | Proxy: streaming, `max_tokens`, tool count, `cache_control`, thinking type, system prompt size | API rules |
| CLAUDE.md | Project directories on disk (skipped with `--no-check-files`; temporary directories never count) | Setup rules |

No prompt content is read for insights; the CLAUDE.md check counts lines.

## Recommendations

Thresholds are heuristics, defined as constants at the top of `insights/rules.py`.

| Id | Fires when | Saving |
|---|---|---|
| `model.big_model_light_sessions` | A Claude Code session (3+ requests) ran its main conversation on Opus or Fable and changed no files | Same tokens repriced on Sonnet 5.5 |
| `model.big_model_subagents` | Subagents ran on Opus or Fable (they inherit the session model unless their definition sets one) | Repriced on Haiku 5.5, an upper bound |
| `effort.high_on_light_work` | A session without edits ran mostly at high/xhigh/max effort and thinking was 40%+ of its output | 50% of the thinking cost, a rough estimate |
| `effort.low_with_struggle` | A session ran mostly at `low` and shows error streaks or file churn | none (quality) |
| `context.kitchen_sink_sessions` | 8+ typed prompts, peak context 150k+ tokens, no reset | Cost of context above 50k tokens, an upper bound |
| `context.near_limit` | Peak context reached 60%+ of the model's window | none |
| `workflow.exploration_in_main_context` | 25+ Read/Grep/Glob calls in the main conversation and no subagent | none |
| `workflow.no_plan_for_large_changes` | 5+ files edited without plan mode (`ExitPlanMode` or the `plan` permission mode) | none |
| `workflow.rework` | A file edited 5+ times, 3+ tool errors in a row, or 3+ interruptions | none |
| `setup.permissions` | 3+ denied tool calls, or 3+ sessions entirely in manual approval mode | none |
| `setup.claude_md_missing` | A project with 3+ sessions has no `CLAUDE.md` or `.claude/CLAUDE.md` | none |
| `setup.claude_md_too_long` | A project's CLAUDE.md files exceed 200 lines | none |
| `cache.avoidable_misses` | The cache doctor found avoidable misses | The avoidable penalty |
| `api.no_prompt_caching` | Proxy conversations resent 4,096+ token prompts with no `cache_control` and no cache reads | Each request reading the previous prompt, minus the first write premium |
| `api.small_outputs_on_big_model` | 10+ proxy requests without tools got under 300 output tokens from Opus or Fable | Repriced on Haiku 5.5, an upper bound |
| `api.high_effort_short_answers` | 10+ proxy requests at high effort or above, short output, mostly thinking | 50% of the thinking cost |
| `api.non_streaming_large_max_tokens` | Non-streaming requests with `max_tokens` above 16,000 | none |
| `api.batch_candidates` | 50+ non-streaming requests without tools per project and day | 50% of their cost if they can wait, an upper bound |
| `workflow.edits_without_commit` | A Claude Code session in a git repository costing $2+ edited files and made no commit in its window ([outcomes](outcomes.md)) | none; severity at most medium, confidence low |
| `model.outcome_cost_per_commit` | Two main models with 5+ git sessions and 5+ commits each differ 1.5x+ in cost per commit | none; info, a correlation |
| `effort.outcome_cost_per_commit` | The same by the most common effort of a session | none; info, a correlation |

**Severity** follows the saving: high from $5 or 5% of spend in range, medium from $1 or 1%,
low below that; findings without a saving carry a fixed severity. **Confidence** says how
directly the data supports the finding: high for measured facts (a missing file, a cache miss),
low for anything that depends on how a model would behave.

**Savings** are API-equivalent list prices of three kinds:

- `reprice`: the same recorded tokens priced differently (a cheaper model, a read instead of a
  write). Another model would produce a different number of tokens.
- `estimate`: an assumption is stated in the recommendation (lower effort halves thinking).
- `upper_bound`: the most that could be saved, if all of it could be avoided.

Recommendations can overlap (a long Opus session can appear under model and context), so the
total in the header can count the same tokens twice. Logs show cost, never quality: a cheaper
model or a lower effort is an experiment to run on real tasks.

The outcome rules use the git outcome of each session ([outcomes.md](outcomes.md)): commits
are a rough proxy for kept work, not for quality, and a gap in cost per commit between models
or effort levels is a correlation (harder tasks tend to get the bigger setting). The profile
shows the totals, and the coach payload carries them as aggregates (`git_outcomes`, no paths).

## Best practices

| Practice | Measured as | Documentation |
|---|---|---|
| Clear context between unrelated tasks | Long sessions (8+ prompts) that grew past 150k tokens without a reset | [best practices](https://code.claude.com/docs/en/best-practices) |
| Match the model to the task | Sessions without edits on Opus or Fable | [costs](https://code.claude.com/docs/en/costs) |
| Run simple subagents on smaller models | Sessions whose subagents ran on Opus or Fable | [subagents](https://code.claude.com/docs/en/sub-agents) |
| Tune effort to the task | Sessions without edits mostly at high effort or above | [model configuration](https://code.claude.com/docs/en/model-config) |
| Use subagents for investigation | Read-heavy sessions without subagents | [best practices](https://code.claude.com/docs/en/best-practices) |
| Plan before multi-file changes | Sessions that changed 5+ files without plan mode | [best practices](https://code.claude.com/docs/en/best-practices) |
| Verify work and course-correct early | Sessions with edits that show rework | [best practices](https://code.claude.com/docs/en/best-practices) |
| Keep a concise CLAUDE.md | Active projects without CLAUDE.md or over 200 lines | [memory](https://code.claude.com/docs/en/memory) |
| Keep the prompt cache warm | Sessions with avoidable cache misses | [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) |
| Cache large repeated prompts (API) | Large proxy requests with no cache use | [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) |
| Stream long generations (API) | Large `max_tokens` without streaming | [streaming](https://platform.claude.com/docs/en/build-with-claude/streaming) |

Status: **pass** below 10% of the measured population, **warn** from 10%, **fail** from 30%,
**n/a** when there is nothing to measure.

## Compare and trends

`--compare` adds a **Since last period** block: the range against the range of the same length
before it, on the measures `trends` tracks per week (cost, context cost, long sessions without
reset, multi-file changes planned, rework, cache hit ratio, avoidable cache penalty, thinking
share), each with a verdict. Both use the predicates and thresholds above
(`is_kitchen_sink`, `is_large_change`, `has_rework` in `insights/rules.py`). See
[trends.md](trends.md).

## What-if

`whatif` reprices the selected requests on another model: `--from` filters by model name,
`--part main` keeps the main conversations, `--part subagents` only subagent requests. Requests
already on the target model are skipped. It is the price of an experiment, not its outcome.

## Coach

`--coach` sends the report to Claude and prints a short plan: the three changes that matter
most, a model and effort plan, what you already do well, and what to measure next time.

- **What is sent:** the usage profile (counts, cost shares per model, effort and interface,
  thinking share, cache hit ratio), each recommendation's id, title, finding, recommendation,
  saving and confidence, and the practice scorecard. **Not sent:** session ids, project paths,
  evidence lists, and of course no prompts (claude-lens never stores them). `--dry-run` prints
  the exact payload. With `--compare` it also gets `since_last_period` (both ranges' metrics and
  verdicts), and the coach says what improved and what got worse.
- **Model:** `LENS_COACH_MODEL` (default `claude-opus-5-5`) at `LENS_COACH_EFFORT` (default
  `medium`), with server-side fallbacks (`fallbacks: "default"`) on models that support them. A
  refusal is reported as an error.
- **Language:** `--lang` or `LENS_COACH_LANGUAGE` (default English).
- **Backend** (`LENS_COACH_BACKEND`, default `auto`):
  - `api`: the Claude API through the Anthropic SDK, with its own credential resolution
    (`ANTHROPIC_API_KEY`, an auth token, or an `ant auth login` profile). Billed per token: one
    request with a few thousand input tokens costs cents.
  - `claude-code`: the local Claude Code CLI (`claude -p`, binary from `LENS_COACH_CLAUDE_BIN`)
    with every tool turned off (`--tools ""`), the coach's own system prompt, no saved session,
    run in an empty temporary directory so no project CLAUDE.md, settings or hooks load. It uses
    whatever Claude Code is signed in with, so a claude.ai subscription works **without an API
    key**; the request counts against the plan's usage limits instead of being billed.
  - `auto`: `api` when the SDK finds credentials, otherwise `claude-code` when the CLI is on
    PATH, otherwise a clear error.
- **Cost:** the CLI prints the model, the backend, token counts and the API-equivalent cost
  (on a subscription nothing is billed per token).

The dashboard has the same coach behind **Ask Claude for a plan**.
