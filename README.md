# petpet-model-registry

The pricing + tier registry consumed by [petpet](https://github.com/ppXD/petpet) clients on a 24-hour cycle. A single `models.json` file: per-model pricing, tier classification, and context-window metadata.

Petpet clients fetch this file via:

```
https://raw.githubusercontent.com/ppXD/petpet-model-registry/main/models.json
```

…parse + validate the schema version, atomically write to `~/.petpet/registry-cache.json`, and pick up the new data on the next app start. If the fetch fails for any reason (404, network, malformed JSON, schema mismatch), clients silently fall back to the version bundled into the binary at build time.

## What this fixes

petpet bundled `models.json` is frozen at build time. New models, repricings, and corrections need a re-release otherwise. The registry decouples model data from the binary so a daily cron can pull from [LiteLLM upstream](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json) and surface diffs as PRs here — no petpet re-release required for users to get the latest rates.

## Schema (v1)

```jsonc
{
  "schema_version": 1,
  "updated_at": "2026-05-18T00:00:00Z",
  "registry_url": "https://github.com/ppXD/petpet-model-registry",

  "fallback_tier_pricing": {
    "frontier":  { "input": 5.00, "output": 25.00, "cache_read": 0.50,
                   "cache_creation": 6.25, "reasoning": 5.00 },
    "mid":       { "input": 1.00, "output":  5.00, "cache_read": 0.10,
                   "cache_creation": 1.25, "reasoning": 1.00 },
    "mini":      { "input": 0.20, "output":  1.00, "cache_read": 0.02,
                   "cache_creation": 0.25, "reasoning": 0.20 },
    "local":     { "input": 0.0,  "output":  0.0,  "cache_read": 0.0,
                   "cache_creation": 0.0,  "reasoning": 0.0  }
  },

  "special_markers": [
    {
      "id": "_free_tier_suffix",
      "match": { "suffix": "-free" },
      "tier": "mini",
      "pricing_per_1m_usd": { "input": 0, "output": 0, "cache_read": 0,
                              "cache_creation": 0, "reasoning": 0 },
      "source": { "type": "convention",
                  "note": "OpenRouter / mirror convention — `-free` always means $0" }
    }
  ],

  "models": [
    {
      "id": "claude-opus-4-7",
      "vendor": "anthropic",
      "family": "claude-opus",
      "tier": "frontier",                  // frontier | mid | mini | local
      "context_window": 200000,
      "max_output_tokens": 32000,
      "match": {
        "substring_keys": ["opus-4-7"],    // ALL substrings must appear
        "exact_aliases":  ["claude-opus-4-7"]  // O(1) hash lookup
      },
      "pricing_per_1m_usd": {
        "input": 5.00, "output": 25.00,
        "cache_read": 0.50, "cache_creation": 6.25,
        "reasoning": 0.0
      },
      "capabilities": ["tools", "vision", "extended_thinking", "prompt_caching"],
      "released": "2026-04",
      "deprecated": false,
      "source": {
        "type": "vendor-official",         // vendor-official | third-party-host
                                           // back-derived | convention
                                           // auto-synced-litellm
        "url":  "https://www.anthropic.com/pricing#api",
        "note": "<one-line provenance>"
      }
    }
  ]
}
```

## Lookup resolution order

petpet clients resolve a model string through these layers, in order:

1. **Special markers** — `-free` suffix → `_free_tier_suffix` (always $0)
2. **Exact alias** — O(1) hash match on `match.exact_aliases`
3. **Substring match** — walk `models[]` in declaration order, return first entry where ALL `match.substring_keys` substrings appear in the normalised model name. Authors must order most-specific first (e.g. `gpt-5-codex` before `gpt-5`).
4. **Miss** — caller falls back to `fallback_tier_pricing[heuristic_tier]`

## Pricing precision

`pricing_per_1m_usd` values are **USD per 1,000,000 tokens**. We keep float, not fixed-point — cents precision is meaningless when most events cost < $0.01 and the upstream rates themselves carry rounding noise.

## How models are added

### Auto-sync from LiteLLM (daily, 09:00 UTC)

A scheduled GitHub Actions workflow:

1. Fetches `model_prices_and_context_window.json` from BerriAI/litellm
2. Maps LiteLLM fields → petpet schema (vendor mapping, per-token → per-1M, capability flags)
3. Auto-classifies tier via the same heuristic petpet's client uses
4. Diffs against current `models.json`
5. For **price changes**: opens a PR labelled `auto-update` — typically auto-mergeable
6. For **new models**: opens a PR labelled `auto-add` + `needs-tier-review` — requires human review of the tier classification

See `scripts/sync_from_litellm.py` and `.github/workflows/sync-daily.yml`.

### Manual additions

Models with no upstream data (e.g. internal / private deployments, brand-new releases before LiteLLM has them) can be added via PR. Use `.github/ISSUE_TEMPLATE/add-model.yml` to request, then submit a PR following `CONTRIBUTING.md`.

### Wrong tier / pricing reports

Use `.github/ISSUE_TEMPLATE/wrong-tier.yml` or `.github/ISSUE_TEMPLATE/pricing-update.yml`.

## Schema versioning policy

- **Minor / additive changes** (new optional field, new capability string): no version bump. Old clients ignore unknown fields via serde defaults.
- **Breaking changes**: bump `schema_version`. Old clients will reject the cache and stay on their bundled snapshot.
- **Major schema change**: we maintain a parallel `models-v{N}.json` for one major version of grace so existing clients don't lose sync.

## License

MIT — see `LICENSE`.

The data here is derived from public vendor pricing pages and the Apache-2.0-licensed LiteLLM project. Vendor-specific terms apply to your usage of the models themselves; this registry only catalogues their published rates.
