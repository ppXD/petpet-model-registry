#!/usr/bin/env python3
"""
Pre-merge validator for models.json.

Run on every PR via .github/workflows/validate.yml; blocks merge on
any failure. Checks the same invariants petpet's client expects so a
bad PR can't break syncing for every user simultaneously.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from typing import Any

SUPPORTED_VENDORS = {
    "anthropic", "openai", "google", "deepseek",
    "meta", "mistral", "xai", "alibaba",
}
SUPPORTED_TIERS = {"frontier", "mid", "mini", "local"}
SUPPORTED_SOURCE_TYPES = {
    "vendor-official", "third-party-host", "back-derived",
    "convention", "auto-synced-litellm",
}
SUPPORTED_CAPABILITIES = {
    "tools", "vision", "audio", "video", "reasoning",
    "prompt_caching", "code", "extended_thinking",
}
SCHEMA_VERSION_PIN = 1
NAME_RE = re.compile(r"^[a-z0-9._-]+$")


def fail(errs: list[str], msg: str) -> None:
    errs.append(msg)


def validate(data: dict[str, Any]) -> list[str]:
    errs: list[str] = []

    if data.get("schema_version") != SCHEMA_VERSION_PIN:
        fail(errs, f"schema_version must be {SCHEMA_VERSION_PIN}, got {data.get('schema_version')}")

    if "fallback_tier_pricing" not in data:
        fail(errs, "missing top-level `fallback_tier_pricing`")
    else:
        for t in ("frontier", "mid", "mini", "local"):
            if t not in data["fallback_tier_pricing"]:
                fail(errs, f"fallback_tier_pricing.{t} missing")

    models_raw = data.get("models")
    if not isinstance(models_raw, list):
        fail(errs, "`models` must be an array")
        return errs

    seen_ids: Counter[str] = Counter()
    real_count = 0
    for i, m in enumerate(models_raw):
        if not isinstance(m, dict):
            fail(errs, f"models[{i}] is not an object")
            continue
        if "_section" in m:
            continue  # divider stub — OK
        real_count += 1

        mid = m.get("id")
        if not isinstance(mid, str) or not NAME_RE.match(mid):
            fail(errs, f"models[{i}].id invalid: {mid!r} (must match {NAME_RE.pattern})")
            continue
        seen_ids[mid] += 1

        if m.get("vendor") not in SUPPORTED_VENDORS:
            fail(errs, f"{mid}: vendor {m.get('vendor')!r} not in {sorted(SUPPORTED_VENDORS)}")
        if m.get("tier") not in SUPPORTED_TIERS:
            fail(errs, f"{mid}: tier {m.get('tier')!r} not in {sorted(SUPPORTED_TIERS)}")
        if not isinstance(m.get("family"), str) or not m["family"]:
            fail(errs, f"{mid}: family missing/empty")
        elif m["family"] == "_TODO_":
            fail(errs, f"{mid}: family is _TODO_ — fill in before merge")

        match = m.get("match")
        if not isinstance(match, dict):
            fail(errs, f"{mid}: match missing/invalid")
        else:
            has_aliases = bool(match.get("exact_aliases"))
            has_subs = bool(match.get("substring_keys"))
            if not (has_aliases or has_subs):
                fail(errs, f"{mid}: at least one of match.exact_aliases / match.substring_keys required")

        pricing = m.get("pricing_per_1m_usd")
        if not isinstance(pricing, dict):
            fail(errs, f"{mid}: pricing_per_1m_usd missing/invalid")
        else:
            for axis in ("input", "output", "cache_read", "cache_creation", "reasoning"):
                v = pricing.get(axis)
                if not isinstance(v, (int, float)) or v < 0:
                    fail(errs, f"{mid}: pricing_per_1m_usd.{axis} must be non-negative number, got {v!r}")

        src = m.get("source")
        if not isinstance(src, dict):
            fail(errs, f"{mid}: source block missing")
        else:
            stype = src.get("type")
            if stype not in SUPPORTED_SOURCE_TYPES:
                fail(errs, f"{mid}: source.type {stype!r} not in {sorted(SUPPORTED_SOURCE_TYPES)}")
            if not isinstance(src.get("note"), str) or not src["note"]:
                fail(errs, f"{mid}: source.note missing/empty")
            url = src.get("url")
            if stype in ("vendor-official", "third-party-host", "auto-synced-litellm"):
                if not isinstance(url, str) or not url.startswith("http"):
                    fail(errs, f"{mid}: source.url required for source.type={stype}")

        caps = m.get("capabilities", [])
        if not isinstance(caps, list):
            fail(errs, f"{mid}: capabilities must be array")
        else:
            for c in caps:
                if c not in SUPPORTED_CAPABILITIES:
                    fail(errs, f"{mid}: unknown capability {c!r}; supported: {sorted(SUPPORTED_CAPABILITIES)}")

    dupes = [mid for mid, n in seen_ids.items() if n > 1]
    if dupes:
        fail(errs, f"duplicate ids: {dupes}")

    if real_count == 0:
        fail(errs, "models[] has zero real entries")

    return errs


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "models.json"
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"✗ {path}: {e}", file=sys.stderr)
        return 1

    errs = validate(data)
    if errs:
        print(f"✗ {path}: {len(errs)} validation failures:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 1
    real = sum(1 for m in data["models"] if isinstance(m, dict) and "_section" not in m)
    print(f"✓ {path}: schema_version={data['schema_version']}, {real} models validated", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
