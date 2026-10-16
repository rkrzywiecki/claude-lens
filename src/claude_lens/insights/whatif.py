"""What-if repricing: the same recorded tokens on another model or at another speed.

This answers "what would this traffic have cost on model X", not "what would model X have
done": a different model produces a different number of tokens (other tokenizers count the
same text differently, and output length and thinking change with the model), and its quality
on the task is unknown. Treat the result as the price of the experiment, not its outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic import BaseModel, computed_field

from claude_lens.models import RequestRecord
from claude_lens.pricing import Pricing, normalize_model


class WhatIf(BaseModel):
    target: str
    requests: int
    current_cost: float
    target_cost: float
    unpriced_requests: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def savings(self) -> float:
        return self.current_cost - self.target_cost


def reprice(
    records: Iterable[RequestRecord],
    pricing: Pricing,
    target: str,
    select: Callable[[RequestRecord], bool] = lambda _: True,
) -> WhatIf:
    """Cost of the selected records as recorded, and with every one priced on `target`."""
    if pricing.price_for(target) is None:
        raise ValueError(f"no price for target model {target!r}")
    current = projected = 0.0
    requests = unpriced = 0
    for record in records:
        if not select(record) or normalize_model(record.model) == normalize_model(target):
            continue
        now = pricing.cost(record.model, record.usage, record.speed)
        then = pricing.cost(target, record.usage)
        if now is None or then is None:
            unpriced += 1
            continue
        requests += 1
        current += now.total
        projected += then.total
    return WhatIf(
        target=target,
        requests=requests,
        current_cost=current,
        target_cost=projected,
        unpriced_requests=unpriced,
    )
