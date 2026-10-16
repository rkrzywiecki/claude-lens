"""Price table (pricing.yaml) and API-equivalent cost of a request."""

from __future__ import annotations

import re
from importlib import resources
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_lens.models import CostBreakdown, Speed, Usage

PER_TOKEN = 1 / 1_000_000
_DATE_SUFFIX = re.compile(r"-\d{8}$")


class PricingError(ValueError):
    """The price table cannot be read or is invalid."""


class TierPrice(BaseModel):
    """Prices in USD per million tokens; cache_read defaults to 0.1x input."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_read: float | None = Field(default=None, ge=0)

    @property
    def cache_read_price(self) -> float:
        return self.cache_read if self.cache_read is not None else self.input * 0.1


class LongContextPrice(TierPrice):
    threshold_tokens: int = Field(gt=0)


class ModelPrice(TierPrice):
    fast_multiplier: float = Field(default=1.0, ge=1)
    long_context: LongContextPrice | None = None
    # Not a price, but per-model like one: used to show how full the context got.
    context_window: int | None = Field(default=None, gt=0)

    def tier(self, prompt_tokens: int) -> TierPrice:
        if self.long_context and prompt_tokens > self.long_context.threshold_tokens:
            return self.long_context
        return self


class Pricing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_write_5m_multiplier: float = Field(default=1.25, ge=0)
    cache_write_1h_multiplier: float = Field(default=2.0, ge=0)
    models: dict[str, ModelPrice]

    def context_window(self, model: str) -> int | None:
        price = self.price_for(model)
        return price.context_window if price else None

    def price_for(self, model: str) -> ModelPrice | None:
        return self.models.get(model) or self.models.get(normalize_model(model))

    def cost(self, model: str, usage: Usage, speed: Speed = "standard") -> CostBreakdown | None:
        """Cost of one request, or None when the model has no price."""
        price = self.price_for(model)
        if price is None:
            return None
        tier = price.tier(usage.prompt_tokens)
        scale = PER_TOKEN * (price.fast_multiplier if speed == "fast" else 1.0)
        return CostBreakdown(
            input=usage.input_tokens * tier.input * scale,
            output=usage.output_tokens * tier.output * scale,
            cache_read=usage.cache_read_tokens * tier.cache_read_price * scale,
            cache_write=(
                usage.cache_write_5m_tokens * self.cache_write_5m_multiplier
                + usage.cache_write_1h_tokens * self.cache_write_1h_multiplier
            )
            * tier.input
            * scale,
            without_cache=(usage.prompt_tokens * tier.input + usage.output_tokens * tier.output)
            * scale,
        )


def normalize_model(model: str) -> str:
    """Drop a dated snapshot suffix: claude-haiku-4-5-20251001 -> claude-haiku-4-5."""
    return _DATE_SUFFIX.sub("", model)


def load_pricing(path: Path | None = None) -> Pricing:
    """Load the price table from `path`, or the one bundled with the package."""
    try:
        return parse_pricing(pricing_text(path))
    except (OSError, PricingError) as exc:
        raise PricingError(f"cannot load pricing from {path or 'package defaults'}: {exc}") from exc


def pricing_text(path: Path | None = None) -> str:
    """Raw YAML of the price table at `path`, or of the bundled one."""
    if path is None:
        return resources.files("claude_lens").joinpath("data/pricing.yaml").read_text("utf-8")
    return path.read_text("utf-8")


def parse_pricing(text: str) -> Pricing:
    try:
        return Pricing.model_validate(yaml.safe_load(text))
    except (yaml.YAMLError, ValidationError) as exc:
        raise PricingError(f"invalid price table: {exc}") from exc
