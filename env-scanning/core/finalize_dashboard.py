#!/usr/bin/env python3
"""
Dashboard Finalization Pipeline — LLM 할루시네이션 원천봉쇄
==========================================================
"계산은 Python이, 판단은 LLM이" — v3.9.0

LLM에게 1개 명령만 요청하면, Python이 SOT에서 모든 경로를 자동 해석하여
전체 대시보드 파이프라인을 수행한다.

WHY: LLM이 dashboard_data_extractor.py CLI를 직접 구성할 때:
  - --integrated-report 파라미터를 누락 → 4개 탭 비어짐
  - --status-file 경로 오류 → KPI 데이터 소실
  - 5개 이상의 파라미터를 정확히 기억해야 함
  → 이 스크립트는 LLM의 파라미터 전달 부담을 date + registry 2개로 축소

Pipeline:
    LLM → "python3 finalize_dashboard.py --date 2026-04-03 --registry workflow-registry.yaml"
        ↓ (Python 내부 자동)
    1. SOT에서 모든 경로 해석
    2. master-status-{date}.json 자동 탐색
    3. integrated-scan-{date}.md 자동 탐색 (EN + KO)
    4. dashboard_data_extractor 호출 (모든 파라미터 자동)
    5. dashboard_generator 호출
    6. validate_dashboard 호출
    7. dashboard archive 복사

Usage:
    python3 env-scanning/core/finalize_dashboard.py \\
        --date 2026-04-03 \\
        --registry env-scanning/config/workflow-registry.yaml

Exit codes:
    0: SUCCESS — dashboard generated and validated
    1: FAIL    — critical error (missing files, validation failure)
    2: WARN    — dashboard generated but validation has warnings
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "1.0.0"

try:
    import yaml
    _YAML = True
except ImportError:
    _YAML = False


def _load_yaml(path: Path) -> dict:
    if not _YAML:
        print("ERROR: PyYAML required. Install with: pip install pyyaml", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _run(cmd: list[str], label: str) -> int:
    """Run a subprocess and print its output. Returns exit code."""
    print(f"\n{'─' * 60}")
    print(f"  [{label}]")
    print(f"  CMD: {' '.join(cmd[:3])}...")
    print(f"{'─' * 60}")
    result = subprocess.run(cmd, capture_output=False, text=True)
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dashboard Finalization Pipeline — LLM 할루시네이션 원천봉쇄 (v3.9.0)"
    )
    parser.add_argument("--date", required=True, help="Scan date (YYYY-MM-DD)")
    parser.add_argument("--registry", required=True, help="Path to workflow-registry.yaml (SOT)")
    args = parser.parse_args()

    date = args.date
    registry_path = Path(args.registry)
    if not registry_path.exists():
        print(f"ERROR: SOT not found: {registry_path}", file=sys.stderr)
        sys.exit(1)

    # ── Step 1: Read SOT and resolve all paths ──
    # SOT paths are relative to PROJECT ROOT (git repo root).
    sot = _load_yaml(registry_path)
    env_scanning_root = registry_path.parent.parent   # → env-scanning/
    project_root = env_scanning_root.parent            # → project root
    system = sot.get("system", {})
    integration = sot.get("integration", {})

    # Integration output root (SOT: "env-scanning/integrated")
    int_output_str = integration.get("output", {}).get("root", "env-scanning/integrated")
    int_output_root = project_root / int_output_str
    reports_daily = int_output_root / "reports" / "daily"
    analysis_dir = int_output_root / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)

    # Dashboard config from SOT
    dashboard_cfg = integration.get("dashboard", {})
    extractor_script = env_scanning_root / "core" / "dashboard_data_extractor.py"
    generator_script = env_scanning_root / "core" / "dashboard_generator.py"
    validator_script = env_scanning_root / "scripts" / "validate_dashboard.py"

    # Archive path
    year, month = date[:4], date[5:7]
    archive_dir = int_output_root / "reports" / "dashboard-archive" / year / month
    archive_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 2: Auto-discover required files ──
    master_status = int_output_root / "logs" / f"master-status-{date}.json"
    integrated_report_en = reports_daily / f"integrated-scan-{date}.md"
    integrated_report_ko = reports_daily / f"integrated-scan-{date}-ko.md"
    dashboard_data_path = analysis_dir / f"dashboard-data-{date}.json"
    dashboard_html_path = reports_daily / f"dashboard-{date}.html"

    # Validate required files exist
    missing = []
    if not master_status.exists():
        missing.append(f"master-status: {master_status}")
    if not integrated_report_en.exists():
        missing.append(f"integrated-report (EN): {integrated_report_en}")

    if missing:
        print("=" * 60)
        print("  ERROR: Required files not found:")
        for m in missing:
            print(f"    ✗ {m}")
        print("=" * 60)
        sys.exit(1)

    print("=" * 60)
    print(f"  Dashboard Finalization Pipeline v{VERSION}")
    print(f"  Date: {date}")
    print(f"  SOT: {registry_path}")
    print(f"  Master Status: {master_status}")
    print(f"  Integrated Report: {integrated_report_en}")
    print(f"  KO Report: {'✓' if integrated_report_ko.exists() else '✗ (will skip KO narratives)'}")
    print("=" * 60)

    overall_exit = 0

    # ── Step 3: Extract dashboard data ──
    extract_cmd = [
        sys.executable, str(extractor_script),
        "--date", date,
        "--registry", str(registry_path),
        "--status-file", str(master_status),
        "--integrated-report", str(integrated_report_en),
        "--output", str(dashboard_data_path),
    ]
    rc = _run(extract_cmd, "Step 3/5: Dashboard Data Extraction")
    if rc == 1:
        print("FATAL: Dashboard data extraction failed.", file=sys.stderr)
        sys.exit(1)
    if rc == 2:
        overall_exit = max(overall_exit, 2)

    # Verify narratives were extracted
    with open(dashboard_data_path, "r", encoding="utf-8") as f:
        dd = json.load(f)
    n_narratives = len(dd.get("narratives", {}))
    if n_narratives == 0:
        print("WARNING: 0 narratives extracted! Tabs 1,7,8,9 will be empty.")
        overall_exit = max(overall_exit, 2)
    else:
        print(f"  ✓ Narratives: {n_narratives} EN + {len(dd.get('narratives_ko', {}))} KO sections")

    # ── Step 4: Generate dashboard HTML ──
    gen_cmd = [
        sys.executable, str(generator_script),
        "--date", date,
        "--data-file", str(dashboard_data_path),
        "--registry", str(registry_path),
        "--output", str(dashboard_html_path),
    ]
    rc = _run(gen_cmd, "Step 4/5: Dashboard HTML Generation")
    if rc == 1:
        print("FATAL: Dashboard generation failed.", file=sys.stderr)
        sys.exit(1)
    if rc == 2:
        overall_exit = max(overall_exit, 2)

    # ── Step 5: Validate dashboard ──
    val_cmd = [
        sys.executable, str(validator_script),
        "--dashboard", str(dashboard_html_path),
        "--date", date,
    ]
    rc = _run(val_cmd, "Step 5/5: Dashboard Validation")
    if rc == 1:
        print("FATAL: Dashboard validation failed.", file=sys.stderr)
        sys.exit(1)
    if rc == 2:
        overall_exit = max(overall_exit, 2)

    # ── Step 6: Archive ──
    archive_path = archive_dir / f"dashboard-{date}.html"
    shutil.copy2(str(dashboard_html_path), str(archive_path))
    print(f"\n  ✓ Archived: {archive_path}")

    # ── Summary ──
    print("\n" + "=" * 60)
    if overall_exit == 0:
        print("  ✓ Dashboard Finalization: SUCCESS")
    else:
        print("  ⚠️  Dashboard Finalization: WARN (non-critical issues)")
    print(f"  Dashboard: {dashboard_html_path}")
    size_kb = dashboard_html_path.stat().st_size / 1024
    print(f"  Size: {size_kb:.0f} KB")
    n_top = sum(len(v) for v in dd.get("top_signals", {}).values())
    print(f"  Signals: {n_top} across {len(dd.get('top_signals', {}))} WFs")
    print(f"  Narratives: {n_narratives} EN sections")
    print("=" * 60)

    sys.exit(overall_exit)


if __name__ == "__main__":
    main()
