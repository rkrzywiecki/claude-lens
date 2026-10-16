---
name: lens
description: Show claude-lens cost, prompt-cache and context recommendations for the last 7 days of Claude Code sessions.
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/scripts/claude-lens insights --days 7)
---

## claude-lens insights (last 7 days)

!`${CLAUDE_PLUGIN_ROOT}/scripts/claude-lens insights --days 7`

Summarize the report above for the user in a few lines: the two or three recommendations with
the largest savings, each with the session evidence it rests on and the concrete next step.
Costs are API-equivalent list prices, not a bill. Do not run other commands. If the report says
claude-lens is not installed, tell the user how to install it instead.
