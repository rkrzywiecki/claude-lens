# Pricing

`claude-lens` reports **API-equivalent** cost: what the recorded tokens cost at Anthropic
first-party list prices. The bundled table is `src/claude_lens/data/pricing.yaml`; set
`LENS_PRICING_PATH` to use your own copy.

## Format

```yaml
cache_write_5m_multiplier: 1.25   # x input price, 5-minute TTL
cache_write_1h_multiplier: 2.0    # x input price, 1-hour TTL

models:
  claude-opus-5-5:
    input: 4.00          # USD per million tokens
    output: 20.00
    cache_read: 0.20     # optional, default 0.1 x input
    fast_multiplier: 2.0 # optional, applied when usage.speed == "fast"
    context_window: 1000000  # optional, not a price: shows how full the context got
  claude-haiku-5-5:
    input: 0.10
    output: 0.50
    long_context:        # optional: other prices above a prompt size
      threshold_tokens: 100000
      input: 0.50
      output: 2.50
```

- Prices are USD per million tokens and must be non-negative. Unknown keys are rejected, so typos
  surface as errors.
- Model ids are looked up as recorded, then with a date suffix removed
  (`claude-haiku-4-5-20251001` uses `claude-haiku-4-5`).
- A long-context tier applies when the request's prompt tokens (input + cache write + cache read)
  exceed `threshold_tokens`. It replaces every price of that request. When the tier omits
  `cache_read`, it defaults to 0.1x the tier's input price.
- `context_window` is used by the session view and insights to show context as a share of the
  window; it does not affect cost.
- A model missing from the table is not priced: its requests are counted and listed as unpriced
  under the report, and the cost of the affected rows is marked with `*`.

## Cost formula

For one request with `scale = 1e-6 x fast_multiplier` (fast mode only):

```
input       = input_tokens          x input
output      = output_tokens         x output
cache_read  = cache_read_tokens     x cache_read
cache_write = (write_5m x 1.25 + write_1h x 2.0) x input
without_cache = (input + write + read tokens) x input + output_tokens x output
cache_savings = without_cache - (input + output + cache_read + cache_write)
```

Not modeled: Batch API discounts, Priority Tier, data-residency or partner-platform pricing,
server-tool fees (web search, code execution hours).
