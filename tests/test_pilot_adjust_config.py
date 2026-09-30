"""Offline regression tests for the immutable Pilot Adjust gate."""

import json
from pathlib import Path

import pytest

import benchmark.real_pilot as real_pilot_module
from benchmark.pilot_adjust_config import (
    DEFAULT_CONFIG_PATH,
    PilotAdjustConfigError,
    load_pilot_adjust_config,
)
from benchmark.pilot_adjust import preflight_adjust_task
from benchmark.real_pilot import build_real_router
from jev_router.cost import CostComponent


EXPECTED_TASKS = ("task_003", "task_004", "task_005", "task_006", "task_008")


def _config_text() -> str:
    return DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pilot_adjust_config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_adjust_config_freezes_timeout_budget_estimates_tasks_and_new_outputs():
    config = load_pilot_adjust_config()

    assert config.phase == "PILOT_ADJUST_GATE"
    assert config.affected_task_ids == EXPECTED_TASKS
    assert config.provider_timeout_seconds == 300.0
    assert config.budget_limit_per_adjust_attempt == 1.50
    assert dict(config.estimated_max_costs) == {
        "low_model": 0.10,
        "medium_model": 0.25,
        "high_model": 1.00,
    }
    assert config.execution_enabled is False
    assert config.preflight_only is True
    assert config.outputs.adjust_results_path == "benchmark/results/adjust_runs.jsonl"
    assert config.outputs.adjust_artifacts_dir == "benchmark/results/adjust_artifacts"


def test_adjust_config_loader_rejects_task_or_parameter_drift(tmp_path):
    replacements = [
        (
            "task list",
            "    - task_008\n",
            "    - task_009\n",
        ),
        (
            "timeout",
            "  provider_timeout_seconds: 300\n",
            "  provider_timeout_seconds: 120\n",
        ),
        (
            "budget",
            "  budget_limit_per_adjust_attempt: 1.50\n",
            "  budget_limit_per_adjust_attempt: 1.25\n",
        ),
        (
            "high estimate",
            "    high_model: 1.00\n",
            "    high_model: 0.99\n",
        ),
        (
            "output",
            "  adjust_results_path: benchmark/results/adjust_runs.jsonl\n",
            "  adjust_results_path: benchmark/results/pilot_runs.jsonl\n",
        ),
    ]

    for label, needle, replacement in replacements:
        changed = _config_text().replace(needle, replacement)
        assert changed != _config_text(), label
        try:
            load_pilot_adjust_config(_write_config(tmp_path, changed))
        except PilotAdjustConfigError:
            continue
        raise AssertionError(f"drift was accepted: {label}")


@pytest.mark.parametrize(
    "timeout_line",
    [
        "  provider_timeout_seconds: 0\n",
        "  provider_timeout_seconds: -1\n",
        "  provider_timeout_seconds: .nan\n",
        "  provider_timeout_seconds: .inf\n",
    ],
)
def test_adjust_config_loader_rejects_invalid_timeout(tmp_path, timeout_line):
    text = _config_text()
    text = text.replace("  provider_timeout_seconds: 300\n", timeout_line)
    with pytest.raises(PilotAdjustConfigError, match="provider_timeout_seconds"):
        load_pilot_adjust_config(_write_config(tmp_path, text))


def test_adjust_runtime_timeout_plumbing_uses_loaded_adjust_config(monkeypatch):
    created = []

    class CapturedProvider:
        def __init__(self, *, timeout):
            created.append(timeout)

    monkeypatch.setattr(real_pilot_module, "OpenCodeGoProvider", CapturedProvider)

    from benchmark.pilot_adjust import build_adjust_runtime_config

    runtime = build_adjust_runtime_config(load_pilot_adjust_config())
    build_real_router(runtime, jev_factory=lambda: _NoCallJEV())

    assert runtime.provider_timeout_seconds == 300.0
    assert created == [300.0, 300.0, 300.0]


class _NoCallJEV:
    def classify(self, _request):
        raise AssertionError("JEV must not be called while building a Router")

    def resolve_cost(self, _request, _classifier):
        return CostComponent(
            cost=0.00004,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        )


def _write_preflight_source(root: Path, config_path: Path):
    results = root / "benchmark" / "results"
    results.mkdir(parents=True)
    (root / "benchmark" / "pilot_config.yaml").write_text("schema_version: 0.1\n", encoding="utf-8")
    summary = {
        "schema_version": "0.1",
        "workflow_gate": "pilot_summary_required",
        "decision": "adjust",
        "total_tasks": 10,
        "real_comparison_tasks": 9,
        "controlled_failure_tasks": 1,
    }
    (results / "pilot_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (results / "pilot_summary.md").write_text("# Original Pilot Summary\n\nadjust\n", encoding="utf-8")
    records = []
    for task_id in [f"task_{number:03d}" for number in range(1, 10)]:
        records.extend(
            [
                {"official_pilot": True, "task_id": task_id, "mode": "baseline"},
                {"official_pilot": True, "task_id": task_id, "mode": "router"},
            ]
        )
    records.append({"official_pilot": True, "task_id": "task_010_fallback", "mode": "baseline"})
    (results / "pilot_runs.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_adjust_preflight_accepts_only_affected_task_after_complete_adjust_result(tmp_path):
    config_path = tmp_path / "pilot_adjust_config.yaml"
    config_path.write_text(_config_text(), encoding="utf-8")
    _write_preflight_source(tmp_path, config_path)

    result = preflight_adjust_task(
        "task_003",
        config_path=config_path,
        project_root=tmp_path,
    )

    assert result["status"] == "preflight_ready"
    assert result["task_id"] == "task_003"
    assert result["execution"] == "preflight_only"
    assert result["outputs"]["results_path"] == "benchmark/results/adjust_runs.jsonl"
    assert not (tmp_path / "benchmark" / "results" / "adjust_runs.jsonl").exists()


def test_adjust_preflight_rejects_unaffected_task(tmp_path):
    config_path = tmp_path / "pilot_adjust_config.yaml"
    config_path.write_text(_config_text(), encoding="utf-8")
    _write_preflight_source(tmp_path, config_path)

    with pytest.raises(PilotAdjustConfigError, match="affected"):
        preflight_adjust_task(
            "task_001",
            config_path=config_path,
            project_root=tmp_path,
        )
