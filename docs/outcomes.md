# Outcomes: git commits per session

Logs show what a session cost, not what it produced. claude-lens adds a rough outcome signal:
the commits made in the session's git repository while the session ran.

**Commits are a rough proxy for kept work, not for quality.** A commit says that work was
kept, not that it was good; work committed later, by hand outside the window, or in another
repository is missed. Differences between models or effort levels are correlations (harder
tasks tend to get bigger models), never proof that one is better.

## What is read

For every session whose project directory is a local git repository (temporary directories
are skipped, see `insights/rules.is_project_dir`), claude-lens runs `git log` over the session
window: first request .. last request + 30 minutes. It reads every branch and worktree
(`--all`, without `refs/stash`), skips merge commits and, when `user.email` is set, commits by
other committers. Each commit counts for one session only: the latest one started before it.

Stored per session (table `session_outcomes`, schema v5): commits, files changed, lines added
and removed (`--numstat`, binary files count 0 lines). Commit messages, diffs and file names
are never stored.

Sessions outside git (notes, research, removed directories) are **left out**, not counted as
"no commit". A directory anywhere inside a git work tree counts as that repository, so a notes
folder kept in a repository is judged like code. A removed Claude Code worktree
(`<repo>/.claude/worktrees/<name>`) is read from its main repository.

Git runs read-only: fixed argv, no shell, a timeout (`LENS_GIT_TIMEOUT`, 10 s), no pager,
external diff, textconv or signature checks, optional locks off.

## Cache

Results are cached per repository by a hash of its refs, HEAD and worktrees plus each
session's window end, so re-runs only call git for repositories that moved.

## Where it shows

- `claude-lens report --outcomes [--by day|project|model|effort|session]`: sessions, commits,
  files, +/- lines, sessions with edits but no commit, cost, cost per commit, cost per 100
  changed lines. Sessions count under the model that cost the most / their most common effort.
- `claude-lens session <id>`: a `Git:` line.
- `claude-lens insights`: profile line, rules `workflow.edits_without_commit`,
  `model.outcome_cost_per_commit`, `effort.outcome_cost_per_commit` (at least 5 sessions and 5
  commits per group, a 1.5x gap, confidence low); the coach payload gets aggregates only.
- Dashboard: a "Cost per commit" tile and git facts in the session view.

`--no-git` (or `LENS_GIT=false`) stops claude-lens from running git; outcomes already stored
are still shown.

## Example

`scripts/make_outcome_example.py` builds a git repository with commits at fixed dates and three
synthetic sessions under the git-ignored `examples/outcomes`:

```bash
uv run python scripts/make_outcome_example.py
export LENS_TRANSCRIPTS_DIR=examples/outcomes/transcripts LENS_DB_PATH=examples/outcomes/demo.db
uv run claude-lens report --outcomes --by model
uv run claude-lens session 8e4b
```

The tests (`tests/test_outcomes.py`) do the same with temporary repositories: window and grace
matching, other committers, other branches, overlapping sessions, removed worktrees, numstat
parsing, the refs cache, the v4 -> v5 migration, the rules, CLI and dashboard.
