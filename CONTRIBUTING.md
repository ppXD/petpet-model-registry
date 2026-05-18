# Contributing to petpet-model-registry

The fast path: file an issue with the right template. Most contributions are pricing updates the daily sync workflow already catches.

## I want to add a model

1. Check if the daily sync already opened a PR (filter PRs by `auto-add` label)
2. If not, file an issue via [`add-model`](.github/ISSUE_TEMPLATE/add-model.yml) — that's enough; maintainers will PR it from the issue
3. Or open a PR directly editing `models.json`; see the schema below

## I want to flag wrong tier or stale pricing

- Wrong tier classification: file via [`wrong-tier`](.github/ISSUE_TEMPLATE/wrong-tier.yml)
- Stale pricing: file via [`pricing-update`](.github/ISSUE_TEMPLATE/pricing-update.yml) **with a link to the vendor's pricing page**

## Schema rules (every entry MUST have)

| Field | Required | Notes |
|---|---|---|
| `id` | ✓ | Canonical normalised name. Lowercase, dashes only, no vendor prefix. |
| `vendor` | ✓ | One of: `anthropic`, `openai`, `google`, `deepseek`, `meta`, `mistral`, `xai`, `alibaba`. New vendors require a one-line discussion first. |
| `family` | ✓ | E.g. `claude-opus`, `gpt-5`, `gemini-2`. Groups related model versions. |
| `tier` | ✓ | `frontier` / `mid` / `mini` / `local`. See "Tier classification rubric" below. |
| `match` | ✓ | Either `exact_aliases` or `substring_keys` (or both) populated. |
| `pricing_per_1m_usd` | ✓ | All five axes (`input` / `output` / `cache_read` / `cache_creation` / `reasoning`). Zero is fine, missing is not. |
| `source` | ✓ | `{ type, url, note }`. `type` ∈ `{vendor-official, third-party-host, back-derived, convention, auto-synced-litellm}`. **No entry without provenance.** |
| `context_window` | optional | Max input tokens. |
| `max_output_tokens` | optional | Self-explanatory. |
| `capabilities` | optional | Array, lowercase. Known values: `tools`, `vision`, `audio`, `video`, `reasoning`, `prompt_caching`, `code`, `extended_thinking`. |
| `released` | optional | `YYYY-MM` of public availability. |
| `deprecated` | optional | `true` once vendor sunset. Keeps historical aggregates working without surprising users. |

## Tier classification rubric

We use four tiers; same definitions petpet's heuristic uses internally:

| Tier | Definition | Examples |
|---|---|---|
| `frontier` | Vendor's flagship / largest / hardest-reasoning class. | claude-opus, gpt-5, gemini-2.5-pro, o3/o4, deepseek-reasoner, grok-4, llama-405b |
| `mid` | Balanced / previous-gen flagship / general-purpose. | claude-sonnet, gpt-4o, gemini-2.0-flash, mistral-medium, deepseek-chat |
| `mini` | Cheap / small / instant-class. | claude-haiku, gpt-5-mini, gpt-5-nano, gemini-2.5-flash-lite, deepseek-coder, llama-8b |
| `local` | On-device / self-hosted. Currently treated as `mini` by petpet's client until `Tier::Local` enum variant ships. | ollama/llama-3-8b, local/phi-3 |

**Tier is determined by vendor positioning, NOT by current price.** Vendor repricings shouldn't move a model between tiers — only generational refreshes do.

## Order within `models[]` matters

The petpet client walks `models[]` top-to-bottom for substring matching, **first match wins**. Put most-specific entries first within each vendor block:

```jsonc
{ "id": "gpt-5-codex",  "match": { "substring_keys": ["gpt-5", "codex"] } },  // BEFORE
{ "id": "gpt-5-nano",   "match": { "substring_keys": ["gpt-5-nano"] } },      // these
{ "id": "gpt-5-mini",   "match": { "substring_keys": ["gpt-5-mini"] } },
{ "id": "gpt-5",        "match": { "substring_keys": ["gpt-5"] } }            // ← catch-all last
```

## CI gates on every PR

- `models.json` parses
- `schema_version == 1`
- Every entry has `source.type` ∈ allowed set
- Every entry has `source.url` (except `back-derived` and `convention`)
- No duplicate `id`s
- Pricing is non-negative float

PRs that fail any gate can't merge.

## Daily auto-sync

A scheduled workflow runs at 09:00 UTC:

1. Fetches LiteLLM's `model_prices_and_context_window.json`
2. Maps fields, heuristic-classifies tier
3. Diffs against current `models.json`
4. Opens PRs:
   - **`auto-update`**: pricing change on an existing model. Auto-mergeable if the change is < 50% or matches an upstream pricing-page commit.
   - **`auto-add`** + **`needs-tier-review`**: new model not currently in registry. Maintainer must verify tier classification + decide if it's worth including (LiteLLM has 2700+ entries; many are noise for petpet's users).

See `scripts/sync_from_litellm.py`.

## Bumping schema_version (breaking change)

Don't do this lightly. If you must:

1. Open an RFC issue first describing the breaking change + migration plan
2. After consensus, in the same PR:
   - Bump `schema_version` in `models.json`
   - Copy old `models.json` → `models-v1.json` (frozen for one major-version of grace)
   - Update petpet client's `REGISTRY_SCHEMA_VERSION` const + bundled JSON
3. Tag a release: `v2.0.0`

Old petpet clients will reject the new cache (`schema_version` mismatch) and continue using their bundled snapshot. They can still fetch `models-v1.json` if we keep it published.
