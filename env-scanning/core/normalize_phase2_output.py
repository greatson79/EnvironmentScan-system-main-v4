#!/usr/bin/env python3
"""
Phase 2 Output Normalizer — Field Name 원천봉쇄
================================================
"계산은 Python이, 판단은 LLM이" — v3.9.0

LLM(phase2-analyst)이 생성한 classified-signals JSON의 필드명을
SOT phase2_output_schema에 정의된 표준 형식으로 정규화한다.

WHY: LLM은 동일한 지시를 받아도 필드명을 비결정론적으로 출력한다:
  - "steeps" vs "category" vs "final_category" vs "steeps_category"
  - "E_Economic" vs "E" vs "Economic"
  - "impact": "CRITICAL" vs "impact_score": 9.5

이 스크립트는 어떤 형식이든 수용하고, SOT 표준 형식으로 변환하여
downstream 소비자(priority_score_calculator, dashboard, statistics)가
단일 경로로 데이터를 처리할 수 있도록 보장한다.

Pipeline Position:
    phase2-analyst (LLM) → classified-signals.json (비표준 가능)
                         ↓
    normalize_phase2_output.py (THIS) → classified-signals.json (표준화)
                         ↓
    priority_score_calculator.py → priority-ranked.json

Usage (CLI):
    python3 env-scanning/core/normalize_phase2_output.py \\
        --input  env-scanning/wf1-general/structured/classified-signals-{date}.json \\
        --output env-scanning/wf1-general/structured/classified-signals-{date}.json

Usage (importable):
    from core.normalize_phase2_output import normalize_classified_signals
    data = normalize_classified_signals(raw_data)

Exit codes:
    0: SUCCESS — all signals normalized, no data loss
    2: WARN   — some signals had missing fields (filled with defaults)
"""

import argparse
import json
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# STEEPs normalization table
# ---------------------------------------------------------------------------

# Full name → single code mapping
_STEEPS_FULL_TO_CODE = {
    "social": "S", "s_social": "S",
    "technological": "T", "t_technological": "T",
    "economic": "E", "e_economic": "E",
    "environmental": "Env", "e_environmental": "Env",
    "political": "P", "p_political": "P",
    "spiritual": "s", "s_spiritual": "s",
}

# Already-valid single codes
_VALID_CODES = {"S", "T", "E", "Env", "P", "s"}


def normalize_steeps_code(raw: str) -> str:
    """Normalize any STEEPs representation to single code.

    Handles:
      "E_Economic" → "E"
      "T_Technological" → "T"
      "Economic" → "E"
      "E" → "E"  (already valid)
      "s_spiritual" → "s"
    """
    if not raw:
        return ""

    raw = raw.strip()

    # Already a valid code
    if raw in _VALID_CODES:
        return raw

    # "E_Economic" pattern: split on underscore, take first part
    if "_" in raw:
        prefix = raw.split("_")[0]
        if prefix in _VALID_CODES:
            return prefix
        # Try full match: "s_spiritual" → "s"
        return _STEEPS_FULL_TO_CODE.get(raw.lower(), raw)

    # Full name: "Economic" → "E"
    return _STEEPS_FULL_TO_CODE.get(raw.lower(), raw)


# ---------------------------------------------------------------------------
# Signal normalization
# ---------------------------------------------------------------------------

def normalize_signal(sig: Dict) -> Dict:
    """Normalize a single signal's field names to SOT schema.

    SOT schema (phase2_output_schema.classified_signals):
      required: id, title, title_ko, category, source
      optional: fssf_type, three_horizons, psst_dimensions, secondary_steeps, keywords
    """
    out = deepcopy(sig)
    warn_count = 0

    # --- category: canonical field ---
    # Priority: category > final_category > steeps > steeps_category > preliminary_category
    if "category" not in out:
        for alt_key in ("final_category", "steeps", "steeps_category", "preliminary_category"):
            if alt_key in out:
                out["category"] = out[alt_key]
                break

    # Normalize code format
    if "category" in out:
        out["category"] = normalize_steeps_code(str(out["category"]))
    else:
        out["category"] = ""
        warn_count += 1

    # --- secondary_steeps: normalize codes ---
    if "secondary_steeps" in out:
        out["secondary_steeps"] = [
            normalize_steeps_code(s) for s in out["secondary_steeps"]
        ]

    # --- title_ko: must exist ---
    if not out.get("title_ko"):
        out["title_ko"] = ""
        warn_count += 1

    # --- source: normalize to string if needed ---
    src = out.get("source")
    if isinstance(src, dict):
        # Keep as dict (WF3/WF4 format) — downstream handles both
        pass
    elif src is None:
        out["source"] = ""
        warn_count += 1

    return out, warn_count


def normalize_classified_signals(data: Dict) -> Dict:
    """Normalize entire classified-signals JSON.

    Args:
        data: Raw classified-signals dict (as loaded from JSON)

    Returns:
        Normalized dict with standardized field names
    """
    signals = data.get("signals") or data.get("items") or []
    normalized = []
    total_warns = 0

    for sig in signals:
        norm_sig, warns = normalize_signal(sig)
        normalized.append(norm_sig)
        total_warns += warns

    result = deepcopy(data)
    # Ensure canonical key is "signals"
    result["signals"] = normalized
    if "items" in result and "signals" not in data:
        del result["items"]

    # Add normalization metadata
    result["_normalization"] = {
        "engine": "normalize_phase2_output.py",
        "version": VERSION,
        "normalized_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "signal_count": len(normalized),
        "warn_count": total_warns,
    }

    return result, total_warns


# ---------------------------------------------------------------------------
# CLI Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 2 Output Normalizer — field name 원천봉쇄 (v3.9.0)"
    )
    parser.add_argument(
        "--input", required=True,
        help="Path to classified-signals-{date}.json (input)",
    )
    parser.add_argument(
        "--output", required=True,
        help="Path to write normalized classified-signals JSON (can be same as --input for in-place)",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result, warn_count = normalize_classified_signals(data)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    n = len(result.get("signals", []))
    print("=" * 60)
    print(f"  Phase 2 Output Normalizer v{VERSION}")
    print(f"  Input:  {input_path}")
    print(f"  Output: {output_path}")
    print(f"  Signals normalized: {n}")
    if warn_count > 0:
        print(f"  ⚠️  Warnings: {warn_count} (missing fields filled with defaults)")
    else:
        print(f"  ✓  All fields present — zero warnings")
    print("=" * 60)

    sys.exit(2 if warn_count > 0 else 0)


if __name__ == "__main__":
    main()
