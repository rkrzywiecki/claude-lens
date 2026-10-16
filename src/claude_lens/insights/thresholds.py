"""Context thresholds shared by the insight rules and the live nudges.

A module of plain constants, so the status line and the prompt hook can use them without
importing the analysis code.
"""

from __future__ import annotations

# A session with this many prompts and this much context, never reset, is a "kitchen sink".
KITCHEN_SINK_PROMPTS = 8
KITCHEN_SINK_PEAK_TOKENS = 150_000
# Share of the model's context window past which quality tends to drop.
NEAR_LIMIT_SHARE = 0.6
