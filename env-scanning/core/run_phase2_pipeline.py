#!/usr/bin/env python3
"""
Phase 2 Pipeline Runner — Step 2.3~PG2 연쇄 원천봉쇄
====================================================
"계산은 Python이, 판단은 LLM이" — v3.9.0

LLM(phase2-analyst)이 Step 2.1+2.2를 완료한 후, 이 스크립트가
Step 2.3(priority ranking) ~ Pipeline Gate 2(검증)까지 자동 연쇄 실행한다.

WHY: LLM 오케스트레이터가 Step 2.3을 건너뛰거나 직접 ranked 파일을 생성하면:
  - "signals" 키 사용 (표준: "ranked_signals")
  - 텍스트 점수 출력 ("CRITICAL") (표준: 숫자 psst_score)
  - pSST 6차원 미계산
  → 대시보드, 통계, 진화 추적기 전체에 연쇄 장애 발생

이 스크립트는 LLM이 Step 2.3을 "건너뛸 수 없도록" Python이 강제 실행한다.

Pipeline:
    LLM → "python3 run_phase2_pipeline.py --date 2026-04-03 --workflow wf1-general --registry ..."
        ↓ (Python 내부 자동)
    1. normalize_phase2_output.py — 필드명 정규화
    2. priority_score_calculator.py — pSST 점수 계산
    3. validate_phase2_output.py — PG2 게이트 (12개 검증)
    4. 실패 시 재시도 (max 2)

Usage:
    python3 env-scanning/core/run_phase2_pipeline.py \\
        --date 2026-04-03 \\
        --workflow wf1-general \\
        --registry env-scanning/config/workflow-registry.yaml

Exit codes:
    0: SUCCESS — all steps passed
    1: FAIL    — PG2 CRITICAL failure after retries
    2: WARN    — PG2 passed with warnings
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

VERSION = "1.0.0"
MAX_RETRIES = 2

try:
    import yaml
    _YAML = True
except ImportError:
    _YAML = False


def _load_yaml(path: Path) -> dict:
    if not _YAML:
        print("ERROR: PyYAML required.", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _run(cmd: list[str], label: str, cwd: str | None = None) -> int:
    """Run subprocess, stream output, return exit code."""
    print(f"\n{'─' * 60}")
    print(f"  [{label}]")
    print(f"{'─' * 60}")
    env = None
    if cwd:
        import os
        env = os.environ.copy()
        env["PYTHONPATH"] = cwd + ":" + env.get("PYTHONPATH", "")
    result = subprocess.run(cmd, capture_output=False, text=True, env=env)
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Phase 2 Pipeline Runner — Step 2.3~PG2 연쇄 원천봉쇄 (v3.9.0)"
    )
    parser.add_argument("--date", required=True, help="Scan date (YYYY-MM-DD)")
    parser.add_argument("--workflow", required=True, help="Workflow name (wf1-general, wf2-arxiv, etc.)")
    parser.add_argument("--registry", required=True, help="Path to workflow-registry.yaml (SOT)")
    args = parser.parse_args()

    date = args.date
    workflow = args.workflow
    registry_path = Path(args.registry)

    if not registry_path.exists():
        print(f"ERROR: SOT not found: {registry_path}", file=sys.stderr)
        sys.exit(1)

    # ── Resolve paths from SOT ──
    # SOT paths like "env-scanning/wf1-general" are relative to PROJECT ROOT
    # (the git repo root), NOT relative to env-scanning/.
    sot = _load_yaml(registry_path)
    # registry is at env-scanning/config/workflow-registry.yaml
    # project root is 2 levels up from config/
    env_scanning_root = registry_path.parent.parent  # → env-scanning/
    project_root = env_scanning_root.parent           # → project root

    # Find workflow data root (SOT path is project-root-relative)
    wf_config = sot.get("workflows", {}).get(workflow, {})
    data_root_str = wf_config.get("data_root", f"env-scanning/{workflow}")
    data_root = project_root / data_root_str

    # Resolve file paths
    classified_path = data_root / "structured" / f"classified-signals-{date}.json"
    impact_path = data_root / "analysis" / f"impact-assessment-{date}.json"
    filtered_path = data_root / "filtered" / f"new-signals-{date}.json"
    ranked_output = data_root / "analysis" / f"priority-ranked-{date}.json"
    thresholds_path = env_scanning_root / "config" / "thresholds.yaml"

    # Script paths
    normalizer = env_scanning_root / "core" / "normalize_phase2_output.py"
    calculator = env_scanning_root / "core" / "priority_score_calculator.py"
    pg2_validator = env_scanning_root / "scripts" / "validate_phase2_output.py"

    # Validate inputs
    if not classified_path.exists():
        print(f"ERROR: classified-signals not found: {classified_path}", file=sys.stderr)
        print("  → Phase 2.1+2.2 must complete before running this pipeline.", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print(f"  Phase 2 Pipeline Runner v{VERSION}")
    print(f"  Date: {date}")
    print(f"  Workflow: {workflow}")
    print(f"  Classified: {classified_path}")
    print(f"  Impact: {'✓' if impact_path.exists() else '✗ (optional)'}")
    print(f"  Filtered: {'✓' if filtered_path.exists() else '✗ (optional)'}")
    print("=" * 60)

    # priority_score_calculator.py uses "from core.psst_calculator import ..."
    # so PYTHONPATH must include env-scanning/ directory
    pythonpath_dir = str(env_scanning_root)
    overall_exit = 0

    # ── Step 1: Normalize field names ──
    norm_cmd = [
        sys.executable, str(normalizer),
        "--input", str(classified_path),
        "--output", str(classified_path),  # in-place normalization
    ]
    rc = _run(norm_cmd, "Step 1/3: Field Name Normalization")
    if rc == 1:
        print("FATAL: Normalization failed.", file=sys.stderr)
        sys.exit(1)
    if rc == 2:
        overall_exit = max(overall_exit, 2)

    # ── Step 2: Priority Score Calculation (with retry) ──
    calc_cmd = [
        sys.executable, str(calculator),
        "--classified", str(classified_path),
        "--impact", str(impact_path) if impact_path.exists() else "",
        "--filtered", str(filtered_path) if filtered_path.exists() else "",
        "--thresholds", str(thresholds_path),
        "--workflow", workflow,
        "--date", date,
        "--output", str(ranked_output),
    ]
    # Remove empty args
    calc_cmd = [c for c in calc_cmd if c]

    for attempt in range(1, MAX_RETRIES + 2):
        rc = _run(calc_cmd, f"Step 2/3: Priority Score Calculation (attempt {attempt}/{MAX_RETRIES + 1})",
                  cwd=pythonpath_dir)
        if rc in (0, 2):
            if rc == 2:
                overall_exit = max(overall_exit, 2)
            break
        if attempt <= MAX_RETRIES:
            print(f"  ⚠️  Retry {attempt}/{MAX_RETRIES}...")
        else:
            print("FATAL: Priority calculation failed after retries.", file=sys.stderr)
            sys.exit(1)

    # ── Step 3: Pipeline Gate 2 Validation ──
    pg2_cmd = [
        sys.executable, str(pg2_validator),
        "--sot", str(registry_path),
        "--workflow", workflow,
        "--date", date,
    ]
    rc = _run(pg2_cmd, "Step 3/3: Pipeline Gate 2 Validation", cwd=pythonpath_dir)
    if rc == 1:
        print(f"\n  ⚠️  PG2 CRITICAL failure. Ranked output may have issues.")
        print(f"  → Check: {ranked_output}")
        overall_exit = max(overall_exit, 1)
    elif rc == 2:
        overall_exit = max(overall_exit, 2)

    # ── Summary ──
    ranked_exists = ranked_output.exists()
    ranked_size = ranked_output.stat().st_size if ranked_exists else 0

    print("\n" + "=" * 60)
    status = "SUCCESS" if overall_exit == 0 else ("WARN" if overall_exit == 2 else "FAIL")
    print(f"  Phase 2 Pipeline: {status}")
    print(f"  Workflow: {workflow} | Date: {date}")
    print(f"  Ranked output: {ranked_output}")
    print(f"  File size: {ranked_size:,} bytes")

    if ranked_exists:
        with open(ranked_output, "r", encoding="utf-8") as f:
            rd = json.load(f)
        n = len(rd.get("ranked_signals", []))
        top_title = rd.get("ranked_signals", [{}])[0].get("title", "N/A")[:50] if n > 0 else "N/A"
        print(f"  Signals ranked: {n}")
        print(f"  Top signal: {top_title}")
        print(f"  Key: {'ranked_signals ✓' if 'ranked_signals' in rd else 'signals ✗ (legacy)'}")

    print("=" * 60)
    sys.exit(overall_exit)


if __name__ == "__main__":
    main()
