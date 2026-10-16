from pathlib import Path

import pytest

from claude_lens.models import Usage
from claude_lens.pricing import Pricing, PricingError, load_pricing, normalize_model


@pytest.fixture
def pricing() -> Pricing:
    return load_pricing()


def test_bundled_pricing_loads(pricing: Pricing) -> None:
    assert "claude-opus-5-5" in pricing.models


def test_cost_per_token_kind(pricing: Pricing) -> None:
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_5m_tokens=1_000_000,
        cache_write_1h_tokens=1_000_000,
    )
    cost = pricing.cost("claude-opus-5-5", usage)
    assert cost is not None
    assert cost.input == pytest.approx(4.0)
    assert cost.output == pytest.approx(20.0)
    assert cost.cache_read == pytest.approx(0.20)
    assert cost.cache_write == pytest.approx(4.0 * 1.25 + 4.0 * 2.0)
    assert cost.without_cache == pytest.approx(4 * 4.0 + 20.0)
    assert cost.cache_savings == pytest.approx(cost.without_cache - cost.total)


def test_cache_read_defaults_to_tenth_of_input(pricing: Pricing) -> None:
    cost = pricing.cost("claude-sonnet-5", Usage(cache_read_tokens=1_000_000))
    assert cost is not None
    assert cost.cache_read == pytest.approx(0.20)


def test_fast_mode_multiplies_every_kind(pricing: Pricing) -> None:
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    fast = pricing.cost("claude-opus-5-5", usage, "fast")
    assert fast is not None
    assert fast.total == pytest.approx(8.0 + 40.0)


def test_fast_without_multiplier_prices_as_standard(pricing: Pricing) -> None:
    usage = Usage(output_tokens=1_000_000)
    assert pricing.cost("claude-sonnet-5", usage, "fast") == pricing.cost("claude-sonnet-5", usage)


def test_long_context_tier(pricing: Pricing) -> None:
    short = pricing.cost("claude-haiku-5-5", Usage(input_tokens=100_000, output_tokens=0))
    long = pricing.cost("claude-haiku-5-5", Usage(input_tokens=100_001, output_tokens=0))
    assert short is not None and long is not None
    assert short.input == pytest.approx(0.01)
    assert long.input == pytest.approx(100_001 * 0.50 / 1_000_000)


def test_dated_model_id_matches_alias(pricing: Pricing) -> None:
    assert normalize_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert pricing.cost("claude-haiku-4-5-20251001", Usage(input_tokens=1)) is not None


def test_unknown_model_has_no_cost(pricing: Pricing) -> None:
    assert pricing.cost("claude-unknown-9", Usage(input_tokens=1)) is None


def test_custom_pricing_file(tmp_path: Path) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text("models:\n  my-model: { input: 1, output: 2 }\n")
    pricing = load_pricing(path)
    cost = pricing.cost("my-model", Usage(output_tokens=1_000_000))
    assert cost is not None and cost.total == pytest.approx(2.0)


@pytest.mark.parametrize(
    "text",
    [
        "models:\n  m: { input: 1 }\n",  # missing output
        "models:\n  m: { input: 1, output: 2, typo: 3 }\n",  # unknown key
        "models:\n  m: { input: -1, output: 2 }\n",  # negative price
        "models: [",  # invalid YAML
    ],
)
def test_invalid_pricing_raises(tmp_path: Path, text: str) -> None:
    path = tmp_path / "pricing.yaml"
    path.write_text(text)
    with pytest.raises(PricingError):
        load_pricing(path)


def test_missing_pricing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(PricingError):
        load_pricing(tmp_path / "nope.yaml")
