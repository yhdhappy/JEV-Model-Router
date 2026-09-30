"""Offline acceptance tests for the official repeat gate."""

import json
from pathlib import Path

import pytest

import benchmark.official_pilot as official
import scripts.official_pilot_task as official_script


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "benchmark" / "fixtures"


def _workflow(tmp_path: Path, *, task_id: str = "task_003", phase: str = "repeat") -> Path:
    state = json.loads((ROOT / "orchestration" / "workflow_state.json").read_text())
    if phase == "repeat":
        state.update({"status": "official_pilot_running", "current_task": "PILOT_REPEAT_GATE"})
        state["pilot_preparation"].update(
            {
                "current_task": task_id,
                "execution_gate": "official_repeat_required",
                "real_pilot_started": True,
            }
        )
    else:
        state.update({"status": "official_pilot_running", "current_task": "PILOT_OFFICIAL_EXECUTION"})
        state["pilot_preparation"].update(
            {
                "execution_gate": "official_pilot_in_progress",
                "real_pilot_started": True,
            }
        )
    path = tmp_path / "workflow_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def _record(task_id: str, mode: str, *, attempt: int = 1, classifier=None, status="manual_passed"):
    record = {
        "official_pilot": True,
        "task_id": task_id,
        "mode": mode,
        "attempt": attempt,
        "source_fixture_hash": official._tree_hash(FIXTURES / task_id),
        "acceptance_status": status,
        "jev_audit": {
            "status": "reviewed" if mode == "router" else "not_applicable",
            "assessment_status": "reviewed" if mode == "router" else "not_applicable",
            "classifier": classifier if mode == "router" else None,
        },
    }
    if attempt > 1:
        record["repeat_authorization"] = {
            "authorized_by": "advisor",
            "parent_attempt": attempt - 1,
            "triggers": ["difficulty_score_near_bucket_boundary", "jev_confidence_below"],
            "timestamp_utc": "2026-09-30T00:00:00Z",
        }
    return record


def _task003_records(*, confidence=0.5, difficulty_score=4, attempt2=False):
    classifier = {"confidence": confidence, "difficulty_score": difficulty_score}
    records = [_record("task_003", "baseline"), _record("task_003", "router", classifier=classifier)]
    if attempt2:
        records.extend(
            [
                _record("task_003", "baseline", attempt=2),
                _record("task_003", "router", attempt=2, classifier=classifier),
            ]
        )
    return records


def _preflight_repeat(monkeypatch, tmp_path, triggers, *, attempt=None, records=None):
    monkeypatch.setattr(official, "PROJECT_ROOT", tmp_path)
    result_path = tmp_path / "benchmark" / "results" / "pilot_runs.jsonl"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in (records or _task003_records())),
        encoding="utf-8",
    )
    key_path = tmp_path / "jev-key"
    key_path.write_text("offline-placeholder", encoding="utf-8")
    monkeypatch.setenv("JEV_API_KEY_FILE", str(key_path))
    return official.preflight_official_repeat(
        "task_003",
        triggers,
        attempt=attempt,
        config_path=ROOT / "benchmark" / "pilot_config.yaml",
        workflow_state_path=_workflow(tmp_path),
        registry_path=ROOT / "config" / "models.real-pilot.yaml",
        fixture_root=FIXTURES,
        results_path=result_path,
    )


def test_repeat_preflight_requires_repeat_phase_and_derives_task003_objective_triggers(monkeypatch, tmp_path):
    preflight = _preflight_repeat(
        monkeypatch,
        tmp_path,
        ["difficulty_score_near_bucket_boundary", "jev_confidence_below"],
    )
    assert preflight.attempt == 2
    assert preflight.repeat_triggers == (
        "difficulty_score_near_bucket_boundary",
        "jev_confidence_below",
    )

    with pytest.raises(official.OfficialPilotGateError, match="workflow_gate_not_ready"):
        official.preflight_official_repeat(
            "task_003",
            ["jev_confidence_below", "difficulty_score_near_bucket_boundary"],
            config_path=ROOT / "benchmark" / "pilot_config.yaml",
            workflow_state_path=_workflow(tmp_path, phase="normal"),
            registry_path=ROOT / "config" / "models.real-pilot.yaml",
            fixture_root=FIXTURES,
            results_path=tmp_path / "benchmark" / "results" / "pilot_runs.jsonl",
        )


@pytest.mark.parametrize(
    "triggers",
    [
        [],
        ["jev_confidence_below"],
        ["difficulty_score_near_bucket_boundary"],
        ["result_dislike", "ad_hoc_rerun"],
    ],
)
def test_task003_repeat_rejects_missing_unmet_or_forbidden_triggers(monkeypatch, tmp_path, triggers):
    records = _task003_records(confidence=0.9, difficulty_score=5)
    with pytest.raises(official.OfficialPilotGateError):
        _preflight_repeat(monkeypatch, tmp_path, triggers, records=records)


def test_repeat_attempts_are_config_bounded_contiguous_and_grouped_by_mode(monkeypatch, tmp_path):
    records = _task003_records(attempt2=True)
    with pytest.raises(official.OfficialPilotGateError, match="attempt"):
        _preflight_repeat(monkeypatch, tmp_path, ["jev_confidence_below", "difficulty_score_near_bucket_boundary"], attempt=2, records=records)
    duplicate = records + [_record("task_003", "router", attempt=2)]
    with pytest.raises(official.OfficialPilotGateError):
        _preflight_repeat(monkeypatch, tmp_path / "duplicate", ["jev_confidence_below", "difficulty_score_near_bucket_boundary"], records=duplicate)

    gap = _task003_records()
    gap.extend([_record("task_003", "baseline", attempt=3), _record("task_003", "router", attempt=3, classifier={"confidence": 0.5, "difficulty_score": 4})])
    with pytest.raises(official.OfficialPilotGateError, match="repeat_attempt_gap"):
        _preflight_repeat(monkeypatch, tmp_path / "gap", ["jev_confidence_below", "difficulty_score_near_bucket_boundary"], records=gap)

    maxed = _task003_records(attempt2=True)
    maxed.extend([_record("task_003", "baseline", attempt=3), _record("task_003", "router", attempt=3, classifier={"confidence": 0.5, "difficulty_score": 4})])
    with pytest.raises(official.OfficialPilotGateError, match="repeat_attempt_limit"):
        _preflight_repeat(monkeypatch, tmp_path / "maxed", ["jev_confidence_below", "difficulty_score_near_bucket_boundary"], records=maxed)


def test_repeat_rejects_pending_parent_jev_audit_and_preserves_protected_files(monkeypatch, tmp_path):
    protected = [
        ROOT / "benchmark" / "results" / "pilot_runs.jsonl",
        ROOT / "orchestration" / "workflow_state.json",
    ]
    before = {path: path.read_bytes() for path in protected}
    records = _task003_records(attempt2=True)
    records[-1]["jev_audit"]["status"] = "pending_human_review"
    records[-1]["jev_audit"]["assessment_status"] = "pending_human_review"
    records[-1]["acceptance_status"] = "manual_passed"
    with pytest.raises(official.OfficialPilotGateError, match="repeat_parent_jev_audit_pending"):
        _preflight_repeat(monkeypatch, tmp_path, ["jev_confidence_below", "difficulty_score_near_bucket_boundary"], records=records)
    assert {path: path.read_bytes() for path in protected} == before


def _runtime_pair(task_id="task_003"):
    base = {
        "official_pilot": False,
        "task_id": task_id,
        "status": "failed",
        "route_status": "failed",
        "route_source": "jev",
        "selected_model": "low_model",
        "classifier": {"confidence": 0.5, "difficulty_score": 4},
        "classifier_metrics": {"exact_cost": 0.123},
        "fallback_history": [],
        "cost": {"classifier_cost": 0.123, "execution_cost": 0.0, "fallback_cost": 0.0, "total_production_cost": 0.123},
        "error_codes": ["executor_failed"],
        "provider_metrics": {},
        "acceptance_status": "pending_manual",
        "source_unchanged": True,
        "workspace_removed": True,
    }
    baseline = dict(base, mode="baseline", route_source="manual_override", classifier=None, classifier_metrics=None)
    router = dict(base, mode="router")
    return {
        "official_pilot": False,
        "task_id": task_id,
        "baseline": baseline,
        "router": router,
        "_evidence": {
            "baseline": {"response_text": "repeat-baseline", "allowed_paths": {}, "error": None},
            "router": {"response_text": "repeat-router", "allowed_paths": {}, "error": None},
        },
    }


def test_execute_repeat_appends_four_task_records_and_preserves_attempt1_artifacts(monkeypatch, tmp_path):
    preflight = _preflight_repeat(
        monkeypatch,
        tmp_path,
        ["difficulty_score_near_bucket_boundary", "jev_confidence_below"],
    )
    attempt1_before = preflight.results_path.read_bytes()
    attempt1_dir = preflight.artifacts_dir / "task_003" / "attempt_1"
    (attempt1_dir / "baseline").mkdir(parents=True)
    marker = attempt1_dir / "baseline" / "marker.txt"
    marker.write_text("attempt-one", encoding="utf-8")
    pair = _runtime_pair()
    monkeypatch.setattr(official, "run_real_pilot_pair", lambda *args, **kwargs: pair)

    result = official.execute_official_repeat(
        "task_003",
        official._issue_official_authorization(),
        preflight,
        timestamp_utc="2026-09-30T00:00:00Z",
    )
    assert result["attempt"] == 2
    records = [json.loads(line) for line in preflight.results_path.read_text().splitlines()]
    task_records = [record for record in records if record["task_id"] == "task_003"]
    assert len(task_records) == 4
    assert b"attempt-one" not in attempt1_before
    assert marker.read_text(encoding="utf-8") == "attempt-one"
    assert all(record["attempt"] == 1 for record in task_records[:2])
    assert all(record["attempt"] == 2 for record in task_records[2:])
    expected_auth = {
        "authorized_by": "advisor",
        "parent_attempt": 1,
        "triggers": ["difficulty_score_near_bucket_boundary", "jev_confidence_below"],
        "timestamp_utc": preflight.repeat_authorization_timestamp_utc,
    }
    assert all(record["repeat_authorization"] == expected_auth for record in task_records[2:])
    assert (preflight.artifacts_dir / "task_003" / "attempt_2" / "baseline").is_dir()
    assert (preflight.artifacts_dir / "task_003" / "attempt_2" / "router").is_dir()


def test_repeat_manual_and_audit_finalizers_target_only_attempt2(monkeypatch, tmp_path):
    preflight = _preflight_repeat(
        monkeypatch,
        tmp_path,
        ["jev_confidence_below", "difficulty_score_near_bucket_boundary"],
    )
    monkeypatch.setattr(official, "run_real_pilot_pair", lambda *args, **kwargs: _runtime_pair())
    official.execute_official_repeat(
        "task_003", official._issue_official_authorization(), preflight,
        timestamp_utc="2026-09-30T00:00:00Z",
    )
    before = [json.loads(line) for line in preflight.results_path.read_text().splitlines()]
    attempt1_before = [record for record in before if record["attempt"] == 1]

    official.finalize_manual_acceptance(
        "task_003", official._issue_official_authorization(), False, True,
        fixture_root=FIXTURES, results_path=preflight.results_path, attempt=2,
    )
    official.finalize_jev_audit_assessment(
        "task_003", official._issue_official_authorization(), "reasonable",
        config_path=ROOT / "benchmark" / "pilot_config.yaml",
        results_path=preflight.results_path, attempt=2,
    )
    after = [json.loads(line) for line in preflight.results_path.read_text().splitlines()]
    assert [record for record in after if record["attempt"] == 1] == attempt1_before
    reviewed = [record for record in after if record["attempt"] == 2]
    assert {record["acceptance_status"] for record in reviewed} == {"manual_failed", "manual_passed"}
    assert next(record for record in reviewed if record["mode"] == "router")["jev_audit"]["assessment"] == "reasonable"


def test_pending_repeat_blocks_task004_until_all_repeat_reviews_are_resolved(monkeypatch, tmp_path):
    monkeypatch.setattr(official, "PROJECT_ROOT", tmp_path)
    result_path = tmp_path / "benchmark" / "results" / "pilot_runs.jsonl"
    result_path.parent.mkdir(parents=True)
    key_path = tmp_path / "jev-key"
    key_path.write_text("offline-placeholder", encoding="utf-8")
    monkeypatch.setenv("JEV_API_KEY_FILE", str(key_path))
    records = []
    for task_id in ("task_001", "task_002", "task_003"):
        records.extend([_record(task_id, "baseline"), _record(task_id, "router")])
    records.extend([
        _record("task_003", "baseline", attempt=2, status="pending_manual"),
        _record("task_003", "router", attempt=2, status="pending_manual"),
    ])
    result_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    with pytest.raises(official.OfficialPilotGateError, match="manual_acceptance_pending"):
        official.preflight_official_task(
            "task_004", config_path=ROOT / "benchmark" / "pilot_config.yaml",
            workflow_state_path=_workflow(tmp_path, phase="normal"),
            registry_path=ROOT / "config" / "models.real-pilot.yaml",
            fixture_root=FIXTURES, results_path=result_path,
        )

    for record in records:
        if record["attempt"] == 2:
            record["acceptance_status"] = "manual_passed"
            record["jev_audit"]["status"] = "reviewed"
            record["jev_audit"]["assessment_status"] = "reviewed"
    result_path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    next_preflight = official.preflight_official_task(
        "task_004", config_path=ROOT / "benchmark" / "pilot_config.yaml",
        workflow_state_path=_workflow(tmp_path, phase="normal"),
        registry_path=ROOT / "config" / "models.real-pilot.yaml",
        fixture_root=FIXTURES, results_path=result_path,
    )
    assert next_preflight.next_task_id == "task_004"


def test_repeat_cli_requires_exact_confirmation_and_refuses_task010(monkeypatch, capsys):
    assert official_script.main(["--task", "task_003", "--repeat"]) == 2
    assert json.loads(capsys.readouterr().out)["error_codes"] == ["repeat_confirmation_required"]
    with pytest.raises(official.OfficialPilotGateError, match="repeat_task_invalid"):
        official.preflight_official_repeat(
            "task_010_fallback", ["jev_confidence_below"],
            config_path=ROOT / "benchmark" / "pilot_config.yaml",
            workflow_state_path=ROOT / "orchestration" / "workflow_state.json",
            registry_path=ROOT / "config" / "models.real-pilot.yaml",
            fixture_root=FIXTURES,
        )
