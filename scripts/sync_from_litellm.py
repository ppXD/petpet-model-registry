#!/usr/bin/env python3
"""
Daily sync: BerriAI/litellm → petpet-model-registry/models.json.

Reads upstream LiteLLM JSON, maps fields to petpet schema, classifies
tier via the same heuristic petpet's client uses, diffs against
current models.json, and writes the proposed update.

CI workflow (.github/workflows/sync-daily.yml) calls this, splits the
diff into `auto-update` (pricing changed on existing entry) and
`auto-add` (new model) commits, and opens labelled PRs.

Run locally:
    python3 scripts/sync_from_litellm.py
        --upstream-url   <override>                     # default: LiteLLM main
        --current        models.json                    # current registry
        --out            models.json.candidate          # write proposed
        --diff           sync-diff.json                 # write diff summary
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from typing import Any

# ─────────────────────────────────────────────────────────────────
# Configuration — petpet-side contracts
# ─────────────────────────────────────────────────────────────────

UPSTREAM_DEFAULT = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# LiteLLM `litellm_provider` → petpet `vendor`. Only these vendors
# survive the curation filter; others (replicate, bedrock, openrouter
# proxies, ...) are noise we don't want polluting the registry.
VENDOR_MAP = {
    "anthropic":  "anthropic",
    "openai":     "openai",
    "azure":      "openai",     # Azure-hosted OpenAI models
    "gemini":     "google",
    "deepseek":   "deepseek",
    "mistral":    "mistral",
    "codestral":  "mistral",
    "xai":        "xai",
    "dashscope":  "alibaba",    # Alibaba Cloud's official API name
}

# Only entries matching this normalised-name pattern are accepted.
# Mirrors petpet's `heuristic::validate_model_name` regex.
NAME_RE = re.compile(r"^[a-z0-9._-]+$")

# Hosted-variant suffixes / labels we don't want flooding the registry.
NOISE_SUFFIXES = ("-tput", "-fp8", "-beta", "-latest", "-preview")

# ─── Tier heuristic — MUST stay in sync with petpet's heuristic.rs ─

MINI_KEYWORDS = {"nano", "mini", "haiku", "small", "lite", "tiny", "lightning"}
MINI_PHRASES = ("flash-lite", "8b-instruct")
MINI_SIZE_TAGS = {"8b", "7b", "3b", "1-5b", "1b", "0-5b"}

FRONTIER_KEYWORDS = {
    "opus", "ultra", "max", "frontier", "pro",
    "o1", "o3", "o4", "o5",
    "reasoner",
}
FRONTIER_PHRASES = ("gpt-5", "gpt-6", "gpt-7", "grok-4", "grok-5", "grok-6")
FRONTIER_SIZE_TAGS = {"70b", "175b", "405b", "671b"}


def fallback_tier(name: str) -> str:
    """Mirror of `petpet::xp::heuristic::fallback_tier`. Returns
    `frontier` / `mid` / `mini`. Never `unknown` — `mid` is the
    Default fallback (caller decides if that means low confidence)."""
    n = name.lower().replace(".", "-").replace("_", "-")
    segs = n.split("-")

    if any(p in n for p in MINI_PHRASES):
        return "mini"
    if any(s in MINI_KEYWORDS for s in segs):
        return "mini"
    if any(s in MINI_SIZE_TAGS for s in segs):
        return "mini"

    if any(p in n for p in FRONTIER_PHRASES):
        return "frontier"
    if any(s in FRONTIER_KEYWORDS for s in segs):
        return "frontier"
    if any(s in FRONTIER_SIZE_TAGS for s in segs):
        return "frontier"

    return "mid"


# ─────────────────────────────────────────────────────────────────
# LiteLLM entry → petpet ModelEntry
# ─────────────────────────────────────────────────────────────────


def strip_known_prefix(key: str) -> str | None:
    """Strip a leading `<vendor>/` if vendor is in our supported set.
    Reject double-slashed paths (e.g. `vertex_ai/xai/grok-4`) to keep
    the registry clean — those are hosted variants we don't promise to
    track."""
    if "/" not in key:
        return key
    prefix, rest = key.split("/", 1)
    if prefix in VENDOR_MAP and "/" not in rest:
        return rest
    return None


def collapse_date_suffix(name: str) -> str:
    """`claude-opus-4-7-20260416` → `claude-opus-4-7`. Mirrors petpet's
    `crate::model::normalize` which strips the trailing all-digits
    suffix (≥8 chars) — LiteLLM also has dashes-in-date variants like
    `gpt-4o-2024-08-06` which we leave alone (only 2 trailing digits)."""
    return re.sub(r"-\d{8,}$", "", name)


def per_1m(value: Any) -> float:
    """Convert LiteLLM's per-token cost (e.g. 1.5e-05) to per-1M USD."""
    if isinstance(value, (int, float)) and value > 0:
        return round(value * 1_000_000, 4)
    return 0.0


def capabilities_for(v: dict) -> list[str]:
    caps: list[str] = []
    if v.get("supports_tool_choice"):
        caps.append("tools")
    if v.get("supports_vision"):
        caps.append("vision")
    if v.get("supports_audio_input") or v.get("supports_audio_output"):
        caps.append("audio")
    if v.get("supports_reasoning"):
        caps.append("reasoning")
    if v.get("supports_prompt_caching"):
        caps.append("prompt_caching")
    return caps


def deprecated_for(v: dict, today: str) -> bool:
    dep = v.get("deprecation_date")
    if isinstance(dep, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", dep):
        return dep < today
    return False


def infer_family(norm: str, known_families: set[str]) -> str:
    """Longest-prefix match against the set of families already in the
    registry. `claude-opus-4-1` matches `claude-opus`, `gemini-2-5-pro`
    matches `gemini-2`, etc. Returns `_TODO_` when nothing matches —
    the validator then blocks the auto-PR until a maintainer fills it
    in (signals "genuinely new family worth thinking about")."""
    candidates = [
        f for f in known_families
        if norm == f or norm.startswith(f + "-")
    ]
    if not candidates:
        return "_TODO_"
    return max(candidates, key=len)


def map_entry(key: str, v: dict, today: str, known_families: set[str] | None = None) -> dict | None:
    """Map one LiteLLM entry → petpet `ModelEntry`. Returns None if the
    entry should be skipped (non-chat mode, unpriced, unsupported
    vendor, namespaced variant, etc.)."""
    if v.get("mode") != "chat":
        return None
    if v.get("litellm_provider") not in VENDOR_MAP:
        return None

    norm = strip_known_prefix(key)
    if norm is None:
        return None
    norm = collapse_date_suffix(norm)
    if not NAME_RE.match(norm):
        return None
    if any(norm.endswith(suf) for suf in NOISE_SUFFIXES):
        return None
    if "preview" in norm:
        return None

    input_cost = v.get("input_cost_per_token", 0)
    output_cost = v.get("output_cost_per_token", 0)
    if not (isinstance(input_cost, (int, float)) and isinstance(output_cost, (int, float))):
        return None
    if input_cost == 0 and output_cost == 0:
        return None

    vendor = VENDOR_MAP[v["litellm_provider"]]
    tier = fallback_tier(norm)

    # OpenAI o-series: reasoning tokens cost = output cost when the
    # field isn't broken out. Mirrors what petpet's algorithm uses.
    reasoning = per_1m(v.get("output_cost_per_reasoning_token"))
    if reasoning == 0 and v.get("supports_reasoning"):
        reasoning = per_1m(output_cost)

    aliases = [norm]
    if key != norm:
        aliases.append(key)

    family = infer_family(norm, known_families or set())

    return {
        "id": norm,
        "vendor": vendor,
        "family": family,  # `_TODO_` if no prefix-matched family known
        "tier": tier,
        "context_window": v.get("max_input_tokens"),
        "max_output_tokens": v.get("max_output_tokens"),
        "match": {
            "substring_keys": [norm],
            "exact_aliases": aliases,
        },
        "pricing_per_1m_usd": {
            "input":          per_1m(input_cost),
            "output":         per_1m(output_cost),
            "cache_read":     per_1m(v.get("cache_read_input_token_cost")),
            "cache_creation": per_1m(v.get("cache_creation_input_token_cost")),
            "reasoning":      reasoning,
        },
        "capabilities": capabilities_for(v),
        "deprecated": deprecated_for(v, today),
        "source": {
            "type": "auto-synced-litellm",
            "url": (
                "https://github.com/BerriAI/litellm/blob/main/"
                "model_prices_and_context_window.json"
            ),
            "note": f"auto-mapped from LiteLLM key {key!r}",
        },
    }


# ─────────────────────────────────────────────────────────────────
# Diff computation
# ─────────────────────────────────────────────────────────────────


def diff_against(current: dict, candidates: dict[str, dict]) -> dict:
    """Compare proposed entries against the current registry.

    Existing entries in `current` are split into:
      - `updates`     — same id, pricing changed
      - `unchanged`   — same id, pricing same
      - `removed`     — id no longer in upstream (we don't auto-delete)
    New ones become `additions`.

    `manual_keep`: entries in `current` whose `source.type` is one of
    {`back-derived`, `vendor-official`, `third-party-host`, `convention`}
    — these are human-curated and the sync MUST NOT overwrite them.
    """
    by_id_current = {
        m["id"]: m for m in current.get("models", [])
        if isinstance(m, dict) and "_section" not in m
    }
    manual_keep_types = {
        "back-derived", "vendor-official", "third-party-host", "convention"
    }

    additions: list[dict] = []
    updates: list[tuple[dict, dict]] = []  # (old, new)
    unchanged: list[str] = []
    skipped_manual: list[str] = []

    for cid, cand in candidates.items():
        cur = by_id_current.get(cid)
        if cur is None:
            additions.append(cand)
            continue
        if cur.get("source", {}).get("type") in manual_keep_types:
            skipped_manual.append(cid)
            continue
        if cur["pricing_per_1m_usd"] == cand["pricing_per_1m_usd"]:
            unchanged.append(cid)
        else:
            updates.append((cur, cand))

    removed = [
        cid for cid in by_id_current
        if cid not in candidates
        and by_id_current[cid].get("source", {}).get("type") == "auto-synced-litellm"
    ]

    return {
        "additions": additions,
        "updates": updates,
        "unchanged": unchanged,
        "removed": removed,
        "skipped_manual": skipped_manual,
    }


# ─────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream-url", default=UPSTREAM_DEFAULT)
    parser.add_argument("--current", default="models.json",
                        help="Current registry file to diff against")
    parser.add_argument("--out", default="models.json.candidate",
                        help="Where to write the proposed merged registry")
    parser.add_argument("--diff", default="sync-diff.json",
                        help="Where to write the diff summary JSON")
    args = parser.parse_args()

    print(f"Fetching upstream: {args.upstream_url}", file=sys.stderr)
    with urllib.request.urlopen(args.upstream_url, timeout=30) as resp:
        upstream = json.load(resp)

    # Load current registry first so we can infer family for new
    # additions via longest-prefix match against existing families.
    with open(args.current) as f:
        current = json.load(f)
    known_families = {
        m["family"] for m in current.get("models", [])
        if isinstance(m, dict) and "_section" not in m and m.get("family", "_TODO_") != "_TODO_"
    }
    print(f"  → {len(known_families)} known families for inference",
          file=sys.stderr)

    today = dt.date.today().isoformat()
    candidates: dict[str, dict] = {}
    for k, v in upstream.items():
        if k == "sample_spec" or not isinstance(v, dict):
            continue
        entry = map_entry(k, v, today, known_families=known_families)
        if entry is None:
            continue
        # Dedup: if same canonical id appears twice (e.g. `gpt-5` and
        # `openai/gpt-5`), prefer the non-namespaced key (which we
        # already stripped to the same `norm`). First-seen wins —
        # upstream order is alphabetical, fine.
        candidates.setdefault(entry["id"], entry)

    print(f"  → {len(candidates)} chat/priced entries after curation",
          file=sys.stderr)

    d = diff_against(current, candidates)
    print(
        f"Diff: +{len(d['additions'])} new,  "
        f"~{len(d['updates'])} pricing-changed,  "
        f"={len(d['unchanged'])} unchanged,  "
        f"-{len(d['removed'])} removed,  "
        f"!{len(d['skipped_manual'])} manual-kept",
        file=sys.stderr,
    )

    # Write candidate (current + additions + updates applied)
    merged = {**current}
    merged["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    by_id = {
        m["id"]: m for m in merged["models"]
        if isinstance(m, dict) and "_section" not in m
    }
    for old, new in d["updates"]:
        # Preserve human-edited family if it's been filled in
        if old.get("family") and old["family"] != "_TODO_":
            new["family"] = old["family"]
        by_id[old["id"]] = new
    for cand in d["additions"]:
        by_id[cand["id"]] = cand
    merged["models"] = [
        m for m in current.get("models", []) if "_section" in m
    ] + sorted(by_id.values(), key=lambda m: (m["vendor"], m["id"]))

    with open(args.out, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
        f.write("\n")

    diff_summary = {
        "ran_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "upstream_url": args.upstream_url,
        "additions": [a["id"] for a in d["additions"]],
        "updates": [
            {
                "id": old["id"],
                "before": old["pricing_per_1m_usd"],
                "after": new["pricing_per_1m_usd"],
            }
            for old, new in d["updates"]
        ],
        "removed_marked_stale": d["removed"],
        "skipped_manual_entries": d["skipped_manual"],
        "unchanged_count": len(d["unchanged"]),
    }
    with open(args.diff, "w") as f:
        json.dump(diff_summary, f, indent=2)
        f.write("\n")

    print(f"Wrote {args.out} + {args.diff}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
