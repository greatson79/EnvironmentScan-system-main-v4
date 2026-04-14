#!/usr/bin/env python3
"""
Context Isolation Manager — Deterministic WF Invocation Engine (원천봉쇄)
=========================================================================
Eliminates LLM hallucination in autopilot WF invocation by computing
EXACT sub-agent prompts, verifying WF results, and recording outcomes
atomically — all without LLM intermediation.

This module is the SINGLE PROGRAMMATIC AUTHORITY for deciding:
  - Whether to invoke a WF as a sub-agent or inline (execution_mode)
  - What EXACT prompt text to pass to the sub-agent (parameter assembly)
  - Which sub-agent type to use (orchestrator name extraction)
  - Whether a completed WF passed its gate check (subprocess validation)
  - What recovery action to take on failure (deterministic decision tree)
  - How to record results in master-status.json (atomic JSON write)

The LLM's role is reduced to EXECUTION ONLY — running this script,
reading the JSON output, and calling Agent() or continuing inline.
No judgment or parameter assembly required from the LLM.

Design Principle:
    "계산은 Python이, 판단은 LLM이."
    WF invocation decisions (prompt assembly, mode branching) = Python.
    Tool invocation (Agent tool call) = LLM.

Usage (CLI):
    # Generate WF invocation instruction (sub-agent prompt or inline flag)
    python3 env-scanning/core/context_isolation_manager.py \\
        --action generate-wf-invocation \\
        --wf wf3-naver \\
        --status-file env-scanning/integrated/logs/master-status-2026-04-03.json \\
        --registry env-scanning/config/workflow-registry.yaml

    # Verify WF result + record in master-status.json atomically
    python3 env-scanning/core/context_isolation_manager.py \\
        --action complete-wf \\
        --wf wf3-naver \\
        --date 2026-04-03 \\
        --status-file env-scanning/integrated/logs/master-status-2026-04-03.json \\
        --registry env-scanning/config/workflow-registry.yaml

Exit codes:
    0 = SUCCESS (instructions written to stdout as JSON)
    1 = ERROR (file read failure, invalid arguments, missing WF, etc.)

Version: 1.0.0
Created: 2026-04-04
Origin: Autopilot context exhaustion incident (2026-04-03).
        Single master-orchestrator agent exhausted context after WF2,
        silently stopping the pipeline. Sub-agent isolation prevents this.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


VERSION = "1.0.0"
GENERATOR_ID = "context_isolation_manager.py"

# Gate mapping: WF key → master gate ID
# Must stay in sync with master_task_manager.py MASTER_STEPS and
# master-orchestrator.md gate definitions.
WF_GATE_MAP = {
    "wf1-general": "M1",
    "wf2-arxiv": "M2",
    "wf3-naver": "M2a",
    "wf4-multiglobal-news": "M2b",
}

# Maximum retry count before HALT
MAX_RETRIES = 1


# ---------------------------------------------------------------------------
# Utility functions (shared pattern with master_finalization.py)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_yaml(path: str) -> Dict[str, Any]:
    if yaml is None:
        raise ImportError("PyYAML required: pip install pyyaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_json(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _atomic_json_write(filepath: str, data: Dict[str, Any]) -> None:
    """Write JSON atomically: write to temp file, then os.replace()."""
    dir_path = os.path.dirname(os.path.abspath(filepath))
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False, default=str)
            tmp.write("\n")
        os.replace(tmp_path, filepath)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _resolve_project_root() -> Path:
    """Walk up from this script to find the project root (contains env-scanning/)."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "env-scanning" / "config").exists():
            return current
        current = current.parent
    return Path.cwd()


# ---------------------------------------------------------------------------
# SOT Parameter Resolution
# ---------------------------------------------------------------------------

def _resolve_wf_params(
    registry: Dict[str, Any],
    wf_key: str,
    scan_window: Dict[str, Any],
    bilingual_config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Resolve all parameters needed for a WF invocation from SOT + state files.

    Returns None if WF is not found or disabled.
    """
    wf_cfg = registry.get("workflows", {}).get(wf_key)
    if not wf_cfg:
        return None
    if not wf_cfg.get("enabled", False):
        return None

    system_cfg = registry.get("system", {})
    tc_cfg = system_cfg.get("temporal_consistency", {})
    bi_cfg = system_cfg.get("bilingual", {})
    shared_cfg = system_cfg.get("shared_invariants", {})

    # Bilingual overrides (from bilingual_resolver.py output)
    bi_wf = bilingual_config.get("workflows", {}).get(wf_key, {})
    internal_lang = bilingual_config.get("internal_language", "en")

    # Resolve validate_profile and skeleton from bilingual config (priority)
    # or fall back to SOT defaults
    validate_profile = bi_wf.get("validate_profile", wf_cfg.get("validate_profile", "standard"))
    report_skeleton = bi_wf.get("report_skeleton", shared_cfg.get("report_skeleton", ""))

    # Orchestrator path → subagent_type (stem without .md and directory)
    orchestrator_path = wf_cfg.get("orchestrator", "")
    subagent_type = Path(orchestrator_path).stem if orchestrator_path else ""

    # WF-specific parameters
    wf_params = wf_cfg.get("parameters", {})

    return {
        "wf_key": wf_key,
        "wf_name": wf_cfg.get("name", wf_key),
        "data_root": wf_cfg.get("data_root", ""),
        "sources_config": wf_cfg.get("sources_config", ""),
        "validate_profile": validate_profile,
        "report_skeleton": report_skeleton,
        "orchestrator_path": orchestrator_path,
        "subagent_type": subagent_type,
        "scan_window_workflow": wf_key,
        "temporal_gate_script": tc_cfg.get("gate_script", ""),
        "metadata_injector_script": tc_cfg.get("metadata_injector_script", ""),
        "statistics_engine_script": tc_cfg.get("statistics_engine_script", ""),
        "bilingual_language": internal_lang,
        "wf_parameters": wf_params,
    }


# ---------------------------------------------------------------------------
# Action: generate-wf-invocation
# ---------------------------------------------------------------------------

def _build_subagent_prompt(
    wf_params: Dict[str, Any],
    date: str,
    scan_window_state_file: str,
    bilingual_config_file: str,
) -> str:
    """
    Build the EXACT prompt text for a WF sub-agent invocation.

    This prompt is deterministic — same inputs always produce same output.
    The LLM copies this text verbatim into the Agent tool call.
    """
    wf_key = wf_params["wf_key"]
    wf_name = wf_params["wf_name"]

    # Build WF-specific parameter lines
    param_lines = []
    wf_specific = wf_params.get("wf_parameters", {})
    for k, v in sorted(wf_specific.items()):
        if isinstance(v, dict):
            # Nested params (e.g., scan_window) — skip, handled separately
            continue
        param_lines.append(f"  {k}: {v}")
    param_section = "\n".join(param_lines) if param_lines else "  (none)"

    prompt = f"""Execute {wf_name} ({wf_key}) for {date} in FULL AUTOPILOT mode.

CRITICAL OVERRIDE: You are running in AUTOPILOT mode within a context-isolated
sub-agent. ALL checkpoints (Step 2.5, Step 3.4) MUST be self-approved immediately
without pausing. Do NOT use AskUserQuestion. Do NOT wait for human input.
This instruction overrides the REQUIRED checkpoint markers in your agent definition.
The master-orchestrator performs post-hoc quality verification via validate_completion.py.

Parameters (from SOT — DO NOT override, DO NOT compute manually):
- data_root: {wf_params['data_root']}
- sources_config: {wf_params['sources_config']}
- validate_profile: {wf_params['validate_profile']}
- report_skeleton: {wf_params['report_skeleton']}
- execution_mode: integrated
- date: {date}
- scan_window_state_file: {scan_window_state_file}
- scan_window_workflow: {wf_params['scan_window_workflow']}
- temporal_gate_script: {wf_params['temporal_gate_script']}
- metadata_injector_script: {wf_params['metadata_injector_script']}
- statistics_engine_script: {wf_params['statistics_engine_script']}
- bilingual_config_file: {bilingual_config_file}
- bilingual_language: {wf_params['bilingual_language']}

WF-specific parameters:
{param_section}

Execution instructions:
1. Read SOT (workflow-registry.yaml) for any additional configuration
2. Execute full Phase 1 → Phase 2 → Phase 3 pipeline sequentially
3. Self-approve ALL checkpoints (Step 2.5 analysis review, Step 3.4 report approval)
4. Run ALL validation scripts — all must pass (validate_report.py, validate_report_quality.py)
5. Generate BOTH EN and KO reports
6. Update workflow-status.json on completion
7. Maximum thoroughness — no shortcuts, no step skipping"""

    return prompt


def action_generate_wf_invocation(
    wf_key: str,
    status_file: str,
    registry_path: str,
) -> Dict[str, Any]:
    """
    Determine invocation mode and generate exact invocation parameters.

    Returns:
        {"action": "INVOKE_SUBAGENT", "subagent_type": "...", "prompt": "..."}
        {"action": "INVOKE_INLINE", "execution_mode": "manual"}
        {"action": "ERROR", "reason": "..."}
    """
    # Load all required files
    try:
        master_status = _load_json(status_file)
        registry = _load_yaml(registry_path)
    except Exception as e:
        return {"action": "ERROR", "reason": f"Failed to load config files: {e}"}

    # Determine execution mode from master-status.json
    execution_mode = master_status.get("execution_mode", "manual")

    # If manual mode → inline execution (current behavior, no change)
    if execution_mode != "autopilot":
        return {
            "action": "INVOKE_INLINE",
            "execution_mode": execution_mode,
            "reason": f"execution_mode is '{execution_mode}' — use inline WF execution (current behavior)",
        }

    # Load scan-window and bilingual config from master-status references
    scan_window_state_file = master_status.get("scan_window_state_file", "")
    project_root = _resolve_project_root()

    scan_window_path = project_root / scan_window_state_file if scan_window_state_file else None
    scan_window = _load_json(str(scan_window_path)) if scan_window_path and scan_window_path.exists() else {}

    # Derive bilingual config path (pattern: {int_output_root}/logs/bilingual-config-{date}.json)
    bilingual_config_file = master_status.get("bilingual_config_file", "")
    bilingual_config = _load_json(str(project_root / bilingual_config_file)) if bilingual_config_file else {}

    # Extract date from master_id (pattern: quadruple-scan-YYYY-MM-DD)
    master_id = master_status.get("master_id", "")
    date = master_id.replace("quadruple-scan-", "") if "quadruple-scan-" in master_id else ""
    if not date:
        return {
            "action": "ERROR",
            "reason": f"Cannot extract date from master_id '{master_id}'. "
                      f"Expected format: 'quadruple-scan-YYYY-MM-DD'",
        }

    # Resolve WF parameters from SOT + bilingual + scan-window
    wf_params = _resolve_wf_params(registry, wf_key, scan_window, bilingual_config)
    if wf_params is None:
        return {
            "action": "ERROR",
            "reason": f"Workflow '{wf_key}' not found or disabled in SOT",
        }

    # Validate subagent_type
    if not wf_params["subagent_type"]:
        return {
            "action": "ERROR",
            "reason": f"No orchestrator defined for {wf_key} in SOT",
        }

    # Build deterministic prompt
    prompt = _build_subagent_prompt(
        wf_params=wf_params,
        date=date,
        scan_window_state_file=scan_window_state_file,
        bilingual_config_file=bilingual_config_file,
    )

    return {
        "action": "INVOKE_SUBAGENT",
        "subagent_type": wf_params["subagent_type"],
        "prompt": prompt,
        "execution_mode": "autopilot",
        "wf_key": wf_key,
        "wf_name": wf_params["wf_name"],
        "date": date,
    }


# ---------------------------------------------------------------------------
# Action: complete-wf
# ---------------------------------------------------------------------------

def _check_disk_state(
    wf_key: str,
    data_root: str,
    date: str,
    project_root: Path,
) -> Dict[str, Any]:
    """Check WF disk state: workflow-status.json and report files."""
    result = {
        "workflow_status": None,
        "report_exists": False,
        "report_ko_exists": False,
        "report_path": "",
        "report_path_ko": "",
        "signal_count": 0,
        "validation": "",
    }

    # Read workflow-status.json
    ws_path = project_root / data_root / "logs" / "workflow-status.json"
    if ws_path.exists():
        ws = _load_json(str(ws_path))
        result["workflow_status"] = ws.get("status")
        result["signal_count"] = ws.get("signal_count", 0)
        result["validation"] = ws.get("validation", {})

        # Format validation string
        v = ws.get("validation", {})
        if isinstance(v, dict):
            l2a = v.get("l2a", {})
            passed = l2a.get("passed", 0)
            total = l2a.get("total", 0)
            l2a_result = l2a.get("result", "")
            result["validation"] = f"L2a {passed}/{total} {l2a_result}" if total > 0 else ""
        elif isinstance(v, str):
            result["validation"] = v

    # Check report files
    reports_dir = project_root / data_root / "reports" / "daily"

    # Determine report filename pattern per WF
    if wf_key == "wf3-naver":
        report_name = f"naver-scan-{date}.md"
        report_name_ko = f"naver-scan-{date}-ko.md"
    elif wf_key == "wf4-multiglobal-news":
        report_name = f"multiglobal-news-scan-{date}.md"
        report_name_ko = f"multiglobal-news-scan-{date}-ko.md"
    elif wf_key == "wf2-arxiv":
        # WF2 may use environmental-scan or arxiv-scan prefix
        report_name = f"environmental-scan-{date}.md"
        report_name_ko = f"arxiv-scan-{date}-ko.md"
    else:
        report_name = f"environmental-scan-{date}.md"
        report_name_ko = f"environmental-scan-{date}-ko.md"

    report_path = reports_dir / report_name
    report_ko_path = reports_dir / report_name_ko

    # Also check alternative naming patterns
    if not report_path.exists():
        # Try glob for any report with date
        alt_reports = list(reports_dir.glob(f"*{date}*.md"))
        en_reports = [r for r in alt_reports if not r.name.endswith("-ko.md")]
        ko_reports = [r for r in alt_reports if r.name.endswith("-ko.md")]
        if en_reports:
            report_path = en_reports[0]
        if ko_reports:
            report_ko_path = ko_reports[0]

    result["report_exists"] = report_path.exists()
    result["report_ko_exists"] = report_ko_path.exists()
    result["report_path"] = str(report_path.relative_to(project_root)) if report_path.exists() else ""
    result["report_path_ko"] = str(report_ko_path.relative_to(project_root)) if report_ko_path.exists() else ""

    return result


def _run_gate_check(
    wf_key: str,
    date: str,
    registry_path: str,
    project_root: Path,
) -> Dict[str, Any]:
    """
    Run validate_completion.py as subprocess and return structured result.

    This eliminates LLM as intermediary — Python calls Python directly.
    Pattern from master_finalization.py.
    """
    completion_script = project_root / "env-scanning" / "scripts" / "validate_completion.py"

    if not completion_script.exists():
        return {
            "exit_code": 1,
            "error": f"validate_completion.py not found at {completion_script}",
            "checks_passed": 0,
            "total": 0,
        }

    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(completion_script),
                "--sot", registry_path,
                "--date", date,
                "--workflow-only", wf_key,
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(project_root),
        )

        gate_output = {}
        if proc.stdout.strip():
            try:
                gate_output = json.loads(proc.stdout.strip())
            except json.JSONDecodeError:
                gate_output = {"raw_output": proc.stdout.strip()}

        # Parse checks_passed/total from validate_completion.py output.
        # Actual schema: {"status": "PASS", "summary": "9/9 checks passed", "checks": [...]}
        # Extract counts from the checks list (authoritative) or summary string (fallback).
        checks_list = gate_output.get("checks", [])
        checks_passed = sum(1 for c in checks_list if c.get("passed", False))
        total = len(checks_list)

        # Fallback: parse summary string "N/M checks passed"
        if total == 0:
            summary = gate_output.get("summary", "")
            if "/" in summary:
                try:
                    parts = summary.split("/")
                    checks_passed = int(parts[0])
                    total = int(parts[1].split()[0])
                except (ValueError, IndexError):
                    pass

        return {
            "exit_code": proc.returncode,
            "checks_passed": checks_passed,
            "total": total,
            "status": gate_output.get("status", "UNKNOWN"),
            "details": gate_output,
            "stderr": proc.stderr.strip() if proc.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "exit_code": 1,
            "error": "validate_completion.py timed out (120s)",
            "checks_passed": 0,
            "total": 0,
        }
    except Exception as e:
        return {
            "exit_code": 1,
            "error": str(e),
            "checks_passed": 0,
            "total": 0,
        }


def action_complete_wf(
    wf_key: str,
    date: str,
    status_file: str,
    registry_path: str,
) -> Dict[str, Any]:
    """
    Verify WF completion, determine action, and record result atomically.

    This is the complete verify→decide→record pipeline with ZERO LLM intermediation.

    Returns:
        {"action": "PROCEED", "gate": {...}, "recorded": {...}, "master_status_updated": true}
        {"action": "RETRY_FULL", "reason": "..."}
        {"action": "RETRY_PHASE3", "reason": "..."}
        {"action": "HALT", "reason": "..."}
    """
    project_root = _resolve_project_root()

    # Load config
    try:
        master_status = _load_json(status_file)
        registry = _load_yaml(registry_path)
    except Exception as e:
        return {"action": "HALT", "reason": f"Failed to load config: {e}"}

    # Get WF data_root from SOT
    wf_cfg = registry.get("workflows", {}).get(wf_key, {})
    data_root = wf_cfg.get("data_root", "")
    if not data_root:
        return {"action": "HALT", "reason": f"No data_root for {wf_key} in SOT"}

    # Get retry count from master-status
    wf_result = master_status.get("workflow_results", {}).get(wf_key, {})
    retry_count = wf_result.get("retry_count", 0)

    # Step 1: Check disk state
    disk = _check_disk_state(wf_key, data_root, date, project_root)

    # Step 2: Run gate check (Python → Python, no LLM)
    gate = _run_gate_check(wf_key, date, registry_path, project_root)

    # Step 3: Deterministic decision tree
    gate_pass = gate["exit_code"] == 0

    if gate_pass and disk["report_exists"]:
        # SUCCESS: Gate passed and report exists
        gate_id = WF_GATE_MAP.get(wf_key, "")

        # Build result record
        recorded = {
            "status": "completed",
            "report_path": disk["report_path"],
            "report_path_ko": disk["report_path_ko"],
            "signal_count": disk["signal_count"],
            "completed_at": _now_iso(),
            "validation": disk["validation"],
        }

        # Atomic update to master-status.json
        abs_status_file = str(project_root / status_file) if not os.path.isabs(status_file) else status_file
        ms = _load_json(abs_status_file)

        # Update workflow_results
        if "workflow_results" not in ms:
            ms["workflow_results"] = {}
        ms["workflow_results"][wf_key] = recorded

        # Update master_gates
        if "master_gates" not in ms:
            ms["master_gates"] = {}
        ms["master_gates"][gate_id] = {
            "status": "PASS",
            "timestamp": _now_iso(),
            "script": f"validate_completion.py --workflow-only {wf_key}",
            "checks_passed": gate["checks_passed"],
        }

        _atomic_json_write(abs_status_file, ms)

        return {
            "action": "PROCEED",
            "gate": {
                "status": "PASS",
                "gate_id": gate_id,
                "checks_passed": gate["checks_passed"],
                "total": gate["total"],
            },
            "recorded": recorded,
            "master_status_updated": True,
        }

    elif disk["report_exists"] and not gate_pass:
        # Report exists but gate failed — might need Phase 3 fix
        if retry_count < MAX_RETRIES:
            return {
                "action": "RETRY_PHASE3",
                "reason": f"Report exists but gate check failed (exit {gate['exit_code']}). "
                          f"Retry Phase 3 to fix validation issues.",
                "retry_count": retry_count + 1,
                "gate_details": gate.get("details", {}),
                "gate_stderr": gate.get("stderr", ""),
            }
        else:
            return {
                "action": "HALT",
                "reason": f"Report exists but gate check failed after {retry_count} retries. "
                          f"Human intervention required.",
                "gate_details": gate.get("details", {}),
            }

    elif not disk["report_exists"] and disk["workflow_status"] == "completed":
        # Status says completed but no report — inconsistent state
        if retry_count < MAX_RETRIES:
            return {
                "action": "RETRY_FULL",
                "reason": "workflow-status.json shows completed but report file not found. "
                          "Full WF retry needed.",
                "retry_count": retry_count + 1,
            }
        else:
            return {
                "action": "HALT",
                "reason": "workflow-status.json shows completed but report missing after retry. "
                          "Human intervention required.",
            }

    else:
        # No report, WF not completed
        if retry_count < MAX_RETRIES:
            return {
                "action": "RETRY_FULL",
                "reason": f"WF did not complete (status: {disk['workflow_status']}). "
                          f"Full WF retry needed.",
                "retry_count": retry_count + 1,
                "disk_state": disk,
            }
        else:
            return {
                "action": "HALT",
                "reason": f"WF failed after {retry_count} retries "
                          f"(status: {disk['workflow_status']}). Human intervention required.",
            }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Context Isolation Manager — Deterministic WF invocation engine (원천봉쇄)"
    )
    parser.add_argument(
        "--action",
        required=True,
        choices=["generate-wf-invocation", "complete-wf"],
        help="Action to perform",
    )
    parser.add_argument(
        "--wf",
        required=True,
        help="Workflow key (e.g., wf1-general, wf3-naver)",
    )
    parser.add_argument(
        "--date",
        default="",
        help="Scan date (YYYY-MM-DD). Auto-derived from master-status if omitted.",
    )
    parser.add_argument(
        "--status-file",
        required=True,
        help="Path to master-status-{date}.json",
    )
    parser.add_argument(
        "--registry",
        default="env-scanning/config/workflow-registry.yaml",
        help="Path to workflow-registry.yaml (SOT)",
    )

    args = parser.parse_args()

    if args.action == "generate-wf-invocation":
        result = action_generate_wf_invocation(
            wf_key=args.wf,
            status_file=args.status_file,
            registry_path=args.registry,
        )
    elif args.action == "complete-wf":
        # Derive date if not provided
        date = args.date
        if not date:
            ms = _load_json(args.status_file)
            master_id = ms.get("master_id", "")
            date = master_id.replace("quadruple-scan-", "") if "quadruple-scan-" in master_id else ""
        if not date:
            print(json.dumps({"action": "ERROR", "reason": "Cannot determine date. Provide --date or ensure master_id in status file."}))
            sys.exit(1)

        result = action_complete_wf(
            wf_key=args.wf,
            date=date,
            status_file=args.status_file,
            registry_path=args.registry,
        )
    else:
        result = {"action": "ERROR", "reason": f"Unknown action: {args.action}"}

    # Output JSON to stdout
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    # Exit code: 0 for actionable results, 1 for errors
    if result.get("action") == "ERROR":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
