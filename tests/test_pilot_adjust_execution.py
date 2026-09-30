import json
from pathlib import Path

import pytest

from benchmark import pilot_adjust_execution as pae


def test_preflight_is_side_effect_free_and_enforces_state_and_order(tmp_path):
    paths = _fixture_project(tmp_path)
    before = _snapshot(paths["root"])
    result = pae.preflight_adjust_task("task_003", **paths["kwargs"])
    assert result["status"] == "preflight_ready"
    assert result["requires_jev_api_key"] is True
    assert result["writes_performed"] is False
    assert _snapshot(paths["root"]) == before


def test_only_frozen_tasks_and_strict_order(tmp_path):
    paths = _fixture_project(tmp_path)
    with pytest.raises(pae.PilotAdjustExecutionError):
        pae.preflight_adjust_task("task_001", **paths["kwargs"])
    with pytest.raises(pae.PilotAdjustExecutionError):
        pae.preflight_adjust_task("task_004", **paths["kwargs"])


def test_router_only_execution_uses_adjust_runtime_and_separate_outputs(tmp_path, monkeypatch):
    paths = _fixture_project(tmp_path, manual=True)
    calls = []

    class FakeJEV:
        last_call_metrics = {"exact_cost": 0.00004, "request_id": "jev-test"}

        def classify(self, request):
            calls.append(("jev", request))
            return _fake_classifier()

        def resolve_cost(self, request, classifier):
            from jev_router.cost import CostComponent
            return CostComponent(cost=0.00004, cost_estimated=False, cost_estimation_source="provider_usage")

    class FakeProvider:
        last_call_metrics = {"model_id": "medium_model", "provider_reported_cost": 0.25}

        def __init__(self, name):
            self.name = name

        def invoke(self, request):
            calls.append(("provider", self.name, request))
            from jev_router.providers import ModelResponse
            return ModelResponse(text="adjusted response", input_tokens=1, output_tokens=1)

    def jev_factory():
        return FakeJEV()

    def provider_factory(name, _definition):
        return FakeProvider(name)

    original = paths["original_bytes"]
    auth = pae.issue_adjust_authorization()
    result = pae.execute_adjust_task(
        "task_003",
        authorization=auth,
        **paths["kwargs"],
        jev_factory=jev_factory,
        provider_factory=provider_factory,
    )
    assert result["mode"] == "router_adjusted"
    assert result["attempt"] == 1
    assert result["adjusted_runtime"]["provider_timeout_seconds"] == 300.0
    assert result["adjusted_runtime"]["budget_limit"] == 1.5
    assert result["adjusted_runtime"]["estimated_max_costs"] == {
        "low_model": 0.1,
        "medium_model": 0.25,
        "high_model": 1.0,
    }
    assert all(item[0] != "baseline" for item in calls)
    assert any(item[0] == "provider" for item in calls)
    assert paths["source_results"].read_bytes() == original
    assert result["artifact"]["path"].startswith("benchmark/results/adjust_artifacts/task_003/")
    assert paths["adjust_results"].is_file()
    assert "adjusted response" in (paths["root"] / result["artifact"]["path"]).joinpath("response.txt").read_text()


def test_duplicate_malformed_and_orphan_outputs_fail_closed(tmp_path):
    paths = _fixture_project(tmp_path)
    paths["adjust_results"].write_text("{\"broken\":", encoding="utf-8")
    with pytest.raises(pae.PilotAdjustExecutionError):
        pae.preflight_adjust_task("task_003", **paths["kwargs"])
    paths = _fixture_project(tmp_path / "second")
    orphan = paths["adjust_artifacts"] / "task_003" / "attempt_1" / "router"
    orphan.mkdir(parents=True)
    with pytest.raises(pae.PilotAdjustExecutionError):
        pae.preflight_adjust_task("task_003", **paths["kwargs"])


def test_manual_acceptance_and_jev_review_are_required_before_next_task(tmp_path):
    paths = _fixture_project(tmp_path, manual=True)
    # This test uses a small synthetic record/artifact to exercise the review gate.
    _write_adjust_record(paths, task_id="task_003", classifier=True, pending=True)
    with pytest.raises(pae.PilotAdjustExecutionError):
        pae.preflight_adjust_task("task_004", **paths["kwargs"])


def test_invalid_confirmation_and_missing_key_are_execution_only(tmp_path, monkeypatch):
    paths = _fixture_project(tmp_path)
    with pytest.raises(pae.PilotAdjustExecutionError):
        pae.execute_adjust_task("task_003", authorization=object(), **paths["kwargs"])
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    with pytest.raises(pae.PilotAdjustExecutionError):
        pae.execute_adjust_task(
            "task_003",
            authorization=pae.issue_adjust_authorization(),
            **paths["kwargs"],
        )
    assert pae.preflight_adjust_task("task_003", **paths["kwargs"])["status"] == "preflight_ready"


def test_manual_and_jev_reviews_unlock_next_task(tmp_path):
    paths = _fixture_project(tmp_path, manual=True)
    result = _execute_with_fakes(paths)
    assert result["acceptance_status"] == "pending_manual"
    with pytest.raises(pae.PilotAdjustExecutionError, match="manual"):
        pae.preflight_adjust_task("task_004", **paths["kwargs"])
    pae.finalize_adjust_manual_acceptance(
        "task_003", True,
        authorization=pae.issue_adjust_authorization(),
        **paths["kwargs"],
    )
    with pytest.raises(pae.PilotAdjustExecutionError, match="JEV"):
        pae.preflight_adjust_task("task_004", **paths["kwargs"])
    pae.finalize_adjust_jev_audit(
        "task_003", "reasonable",
        authorization=pae.issue_adjust_authorization(),
        **paths["kwargs"],
    )
    assert pae.preflight_adjust_task("task_004", **paths["kwargs"])["status"] == "preflight_ready"


def _fixture_project(tmp_path, manual=False):
    root = Path(tmp_path)
    (root / "benchmark/results").mkdir(parents=True)
    (root / "benchmark/pilot_config.yaml").write_text("schema_version: '0.1'\n", encoding="utf-8")
    (root / "benchmark/results/pilot_summary.md").write_text("historical\n", encoding="utf-8")
    summary = {
        "workflow_gate": "pilot_summary_required",
        "decision": "adjust",
        "total_tasks": 10,
        "real_comparison_tasks": 9,
        "controlled_failure_tasks": 1,
    }
    (root / "benchmark/results/pilot_summary.json").write_text(json.dumps(summary) + "\n", encoding="utf-8")
    rows = []
    for i in range(1, 10):
        task = f"task_{i:03d}"
        for mode in ("baseline", "router"):
            rows.append({
                "official_pilot": True, "task_id": task, "mode": mode, "attempt": 1,
                "route_status": "success", "acceptance_status": "passed",
                "selected_model": "high_model", "cost": {"total": 1.0},
            })
    rows.append({"official_pilot": True, "task_id": "task_010_fallback", "mode": "fallback", "attempt": 1})
    source_results = root / "benchmark/results/pilot_runs.jsonl"
    source_results.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    config = Path(__file__).resolve().parents[1] / "benchmark/pilot_adjust_config.yaml"
    local_config = root / "benchmark/pilot_adjust_config.yaml"
    local_config.write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    state = {
        "status": "pilot_adjust_phase1_passed",
        "current_task": "PILOT_ADJUST_EXECUTION_GATE",
        "pilot_preparation": {
            "current_task": "PILOT_ADJUST_EXECUTION_GATE",
            "execution_gate": "adjust_execution_wiring_required",
        },
    }
    state_path = root / "workflow_state.json"
    state_path.write_text(json.dumps(state) + "\n", encoding="utf-8")
    fixture = Path(__file__).resolve().parents[1] / "benchmark/fixtures/task_003"
    kwargs = {
        "config_path": local_config,
        "project_root": root,
        "workflow_state_path": state_path,
        "fixture_root": fixture.parent,
        "registry_path": Path(__file__).resolve().parents[1] / "config/models.real-pilot.yaml",
    }
    adjust_results = root / "benchmark/results/adjust_runs.jsonl"
    adjust_artifacts = root / "benchmark/results/adjust_artifacts"
    return {
        "root": root,
        "kwargs": kwargs,
        "source_results": source_results,
        "adjust_results": adjust_results,
        "adjust_artifacts": adjust_artifacts,
        "original_bytes": source_results.read_bytes(),
    }


def _snapshot(root):
    return sorted((p.relative_to(root).as_posix(), p.read_bytes() if p.is_file() else None) for p in root.rglob("*") if p.is_file())


def _fake_classifier():
    from jev_router.schemas import ClassifierResult
    return ClassifierResult.model_validate({
        "schema_version": "0.1", "task_type": "coding", "difficulty_score": 6,
        "difficulty_bucket": "medium", "required_capability": "medium",
        "confidence": 0.9, "risk_level": "low",
    })


def _write_adjust_record(paths, task_id, classifier, pending):
    # Deliberately malformed artifact-less record: preflight must reject it.
    row = {
        "mode": "router_adjusted", "task_id": task_id, "attempt": 1,
        "adjust_phase": "timeout_fallback_budget_only",
        "acceptance_status": "pending_manual" if pending else "passed",
        "jev_audit": {"status": "pending_human_review"} if classifier else {"status": "not_applicable"},
    }
    paths["adjust_results"].write_text(json.dumps(row) + "\n", encoding="utf-8")


def _execute_with_fakes(paths):
    from jev_router.cost import CostComponent
    from jev_router.providers import ModelResponse
    from jev_router.schemas import ClassifierResult

    class FakeJEV:
        last_call_metrics = {"exact_cost": 0.00004, "request_id": "jev-test"}

        def classify(self, _request):
            return ClassifierResult(
                task_type="coding", difficulty_score=6, difficulty_bucket="medium",
                required_capability="medium", confidence=0.9, risk_level="low",
            )

        def resolve_cost(self, _request, _classifier):
            return CostComponent(cost=0.00004, cost_estimated=False, cost_estimation_source="provider_usage")

    class FakeProvider:
        last_call_metrics = {"model_id": "medium_model", "provider_reported_cost": 0.25}

        def invoke(self, _request):
            return ModelResponse(text="adjusted response", input_tokens=1, output_tokens=1)

    return pae.execute_adjust_task(
        "task_003", authorization=pae.issue_adjust_authorization(), **paths["kwargs"],
        jev_factory=FakeJEV, provider_factory=lambda _name, _definition: FakeProvider(),
    )
