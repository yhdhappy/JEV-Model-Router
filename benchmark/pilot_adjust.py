"""Read-only phase-1 gate for a future Adjust rerun."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from benchmark.pilot_adjust_config import (
    DEFAULT_CONFIG_PATH,
    PilotAdjustConfig,
    PilotAdjustConfigError,
    load_pilot_adjust_config,
)
from benchmark.real_pilot import RealPilotRuntimeConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_TASK_IDS = tuple(f"task_{number:03d}" for number in range(1, 10)) + (
    "task_010_fallback",
)


def build_adjust_runtime_config(config: PilotAdjustConfig) -> RealPilotRuntimeConfig:
    """Translate frozen Adjust values into runtime-only config; never executes."""

    return RealPilotRuntimeConfig(
        baseline_model="high_model",
        budget_limit=config.budget_limit_per_adjust_attempt,
        estimated_max_costs=config.estimated_max_costs,
        provider_timeout_seconds=config.provider_timeout_seconds,
    )


def preflight_adjust_task(
    task_id: str,
    *,
    config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    project_root: Union[str, Path] = PROJECT_ROOT,
) -> Dict[str, Any]:
    """Validate one affected task against completed immutable Pilot evidence.

    This function is intentionally preflight-only. It does not construct a
    provider, read credentials, call a network service, run a fixture, or write
    an Adjust result/artifact.
    """

    config = load_pilot_adjust_config(config_path)
    if not isinstance(task_id, str) or task_id not in config.affected_task_ids:
        raise PilotAdjustConfigError("task_id must be one of the frozen affected tasks")
    root = Path(project_root).expanduser().resolve(strict=True)
    source_root = _validate_completed_source(config, root)
    return {
        "status": "preflight_ready",
        "phase": config.phase,
        "task_id": task_id,
        "execution": "preflight_only",
        "writes_performed": False,
        "source": {
            "decision": config.source.expected_decision,
            "results_path": _relative(config.source.results_path, root),
            "summary_json_path": _relative(config.source.summary_json_path, root),
        },
        "outputs": {
            "results_path": config.outputs.adjust_results_path,
            "artifacts_dir": config.outputs.adjust_artifacts_dir,
            "summary_json_path": config.outputs.adjust_summary_json_path,
            "summary_markdown_path": config.outputs.adjust_summary_markdown_path,
        },
        "frozen_parameters": {
            "provider_timeout_seconds": config.provider_timeout_seconds,
            "budget_limit_per_adjust_attempt": config.budget_limit_per_adjust_attempt,
            "estimated_max_costs": dict(config.estimated_max_costs),
            "affected_task_ids": list(config.affected_task_ids),
        },
        "source_root_verified": source_root,
    }


def _validate_completed_source(config: PilotAdjustConfig, root: Path) -> str:
    official_config = root / config.source.official_config_path
    results_path = root / config.source.results_path
    summary_path = root / config.source.summary_json_path
    markdown_path = root / config.source.summary_markdown_path
    for path in (official_config, results_path, summary_path, markdown_path):
        if not path.is_file():
            raise PilotAdjustConfigError("original Pilot evidence is incomplete")
    try:
        if not official_config.read_text(encoding="utf-8").strip():
            raise PilotAdjustConfigError("original Pilot config is empty")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        records = [
            json.loads(line)
            for line in results_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PilotAdjustConfigError("original Pilot evidence is unreadable") from exc
    if not isinstance(summary, Mapping) or not isinstance(records, list) or not records:
        raise PilotAdjustConfigError("original Pilot evidence is incomplete")
    if (
        summary.get("workflow_gate") != "pilot_summary_required"
        or summary.get("decision") != config.source.expected_decision
        or summary.get("total_tasks") != 10
        or summary.get("real_comparison_tasks") != 9
        or summary.get("controlled_failure_tasks") != 1
    ):
        raise PilotAdjustConfigError("original Pilot summary is not complete with decision Adjust")
    by_task = {}
    for record in records:
        if not isinstance(record, Mapping) or record.get("official_pilot") is not True:
            raise PilotAdjustConfigError("original Pilot results contain an invalid record")
        record_task = record.get("task_id")
        if record_task not in OFFICIAL_TASK_IDS:
            raise PilotAdjustConfigError("original Pilot results contain an unexpected task")
        by_task.setdefault(record_task, set()).add(record.get("mode"))
    for task in OFFICIAL_TASK_IDS[:-1]:
        if not {"baseline", "router"}.issubset(by_task.get(task, set())):
            raise PilotAdjustConfigError("original Pilot results are missing a Baseline/Router pair")
    if not by_task.get("task_010_fallback"):
        raise PilotAdjustConfigError("original controlled fallback result is missing")
    return "verified"


def _relative(path: str, root: Path) -> str:
    return (root / path).relative_to(root).as_posix()


__all__ = ["build_adjust_runtime_config", "preflight_adjust_task"]
