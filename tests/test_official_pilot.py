"""Offline acceptance tests for PILOT_OFFICIAL_EXECUTION_GATE."""

import json
from pathlib import Path

import pytest

import benchmark.official_pilot as official
import scripts.official_pilot_task as official_script
from benchmark.real_pilot import RealPilotConfigurationError, append_jsonl, persist_pair
from jev_router.cost import CostComponent
from jev_router.schemas import ClassifierResult


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "benchmark" / "fixtures"


def _preflight(monkeypatch, tmp_path, task_id, records=(), key=True, phase="A", workflow_overrides=None):
    monkeypatch.setattr(official, "PROJECT_ROOT", tmp_path)
    results = tmp_path / "benchmark" / "results" / "pilot_runs.jsonl"
    results.parent.mkdir(parents=True, exist_ok=True)
    if records:
        results.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
    if key:
        key_path = tmp_path / "jev-key"
        key_path.write_text("offline-test-placeholder", encoding="utf-8")
        monkeypatch.setenv("JEV_API_KEY_FILE", str(key_path))
    else:
        monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    if phase not in {"A", "B"}:
        raise ValueError("test workflow phase must be A or B")
    template_path = ROOT / "orchestration" / "workflow_state.json"
    state = json.loads(template_path.read_text(encoding="utf-8"))
    if phase == "A":
        state["status"] = "pilot_config_frozen"
        state["current_task"] = "PILOT_OFFICIAL_EXECUTION_GATE"
        state["pilot_preparation"]["execution_gate"] = "official_execution_mode_required"
        state["pilot_preparation"]["real_pilot_started"] = False
    else:
        state["status"] = "official_pilot_running"
        state["current_task"] = "PILOT_OFFICIAL_EXECUTION"
        state["pilot_preparation"]["execution_gate"] = "official_pilot_in_progress"
        state["pilot_preparation"]["real_pilot_started"] = True
    for key, value in (workflow_overrides or {}).items():
        if key in {"status", "current_task"}:
            state[key] = value
        else:
            state["pilot_preparation"][key] = value
    workflow_state_path = tmp_path / "workflow_state.json"
    workflow_state_path.write_text(json.dumps(state), encoding="utf-8")
    return official.preflight_official_task(
        task_id,
        config_path=ROOT / "benchmark" / "pilot_config.yaml",
        workflow_state_path=workflow_state_path,
        registry_path=ROOT / "config" / "models.real-pilot.yaml",
        fixture_root=FIXTURES,
    )


def _record(task_id, mode, attempt=1):
    return {"official_pilot": True, "task_id": task_id, "mode": mode, "attempt": attempt}


def _completed_real_tasks():
    return [
        record
        for task_id in official.EXPECTED_REAL_TASK_IDS
        for record in (_record(task_id, "baseline"), _record(task_id, "router"))
    ]


def _safe_pair(task_id="task_001", route_source="light_rule"):
    base = {
        "official_pilot": False,
        "task_id": task_id,
        "status": "passed",
        "route_status": "success",
        "route_source": route_source,
        "selected_model": "low_model",
        "classifier": None,
        "classifier_metrics": None,
        "fallback_history": [],
        "cost": {
            "classifier_cost": 0.0,
            "execution_cost": 0.01,
            "fallback_cost": 0.0,
            "total_production_cost": 0.01,
            "cost_estimated": False,
            "cost_estimation_source": "provider_usage",
        },
        "error_codes": [],
        "provider_metrics": {},
        "acceptance_status": "pending_manual",
        "source_unchanged": True,
        "workspace_removed": True,
    }
    baseline = dict(base, mode="baseline", route_source="manual_override")
    router = dict(base, mode="router")
    return {"official_pilot": False, "task_id": task_id, "baseline": baseline, "router": router}


def _evidence(response="unique-official-answer", allowed_paths=None):
    return {
        "baseline": {"response_text": response, "allowed_paths": allowed_paths or {}, "error": None},
        "router": {"response_text": response, "allowed_paths": allowed_paths or {}, "error": None},
    }


def _execute_fake_task1(monkeypatch, tmp_path, response="unique-official-answer"):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    pair = _safe_pair()
    pair["_evidence"] = _evidence(response)
    monkeypatch.setattr(official, "run_real_pilot_pair", lambda *args, **kwargs: pair)
    result = official.execute_official_task(
        "task_001", official._issue_official_authorization(), preflight,
        audit_jev_factory=lambda: _AuditJEV(_classifier()),
        timestamp_utc="2026-09-30T00:00:00Z",
    )
    return preflight, result


def test_preflight_is_side_effect_free_and_never_constructs_or_calls_network(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    assert preflight.task_id == "task_001"
    assert preflight.next_task_id == "task_001"
    assert preflight.requires_jev_api_key is True
    assert not preflight.results_path.exists()


def test_ordering_and_duplicate_protection(monkeypatch, tmp_path):
    with pytest.raises(official.OfficialPilotGateError, match="workflow_results_mismatch"):
        _preflight(monkeypatch, tmp_path / "phase-b-empty", "task_002", phase="B")

    first_pair = [_record("task_001", "baseline"), _record("task_001", "router")]
    second = _preflight(monkeypatch, tmp_path, "task_002", first_pair, phase="B")
    assert second.next_task_id == "task_002"

    duplicate = first_pair + [_record("task_001", "baseline")]
    with pytest.raises(official.OfficialPilotGateError, match="duplicate_official_record"):
        _preflight(monkeypatch, tmp_path, "task_002", duplicate, phase="B")


def test_workflow_phase_a_and_b_must_match_official_results(monkeypatch, tmp_path):
    assert _preflight(monkeypatch, tmp_path, "task_001").next_task_id == "task_001"
    first_pair = [_record("task_001", "baseline"), _record("task_001", "router")]
    assert _preflight(monkeypatch, tmp_path, "task_002", first_pair, phase="B").next_task_id == "task_002"

    with pytest.raises(official.OfficialPilotGateError, match="workflow_results_mismatch"):
        _preflight(monkeypatch, tmp_path / "phase-b-empty", "task_002", phase="B")
    with pytest.raises(official.OfficialPilotGateError, match="workflow_results_mismatch"):
        _preflight(monkeypatch, tmp_path, "task_002", first_pair, phase="A")

    for overrides in (
        {"status": "wrong"},
        {"current_task": "wrong"},
        {"execution_gate": "wrong"},
        {"real_pilot_started": True},
    ):
        with pytest.raises(official.OfficialPilotGateError, match="workflow_gate_not_ready"):
            _preflight(monkeypatch, tmp_path, "task_001", workflow_overrides=overrides)


def test_missing_key_refuses_execution_before_bridge(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    called = []
    monkeypatch.setattr(official, "run_real_pilot_pair", lambda *args, **kwargs: called.append(True))
    with pytest.raises(official.OfficialPilotGateError, match="jev_api_key_file_required"):
        official.execute_official_task(
            "task_001", official._issue_official_authorization(), preflight
        )
    assert called == []
    assert not preflight.results_path.exists()


def test_explicit_confirmation_is_required_without_preflight_or_execution(monkeypatch, capsys):
    monkeypatch.setattr(official_script, "preflight_official_task", lambda task: pytest.fail("confirmation must be checked first"))
    assert official_script.main(["--task", "task_001", "--execute"]) == 2
    assert json.loads(capsys.readouterr().out)["error_codes"] == ["confirmation_required"]


def test_non_official_apis_cannot_write_official_path(tmp_path):
    official_path = tmp_path / "pilot_runs.jsonl"
    with pytest.raises(RealPilotConfigurationError):
        append_jsonl(official_path, {"official_pilot": True}, allow_official=True)
    pair = {
        "official_pilot": False,
        "baseline": {"official_pilot": False, "mode": "baseline"},
        "router": {"official_pilot": False, "mode": "router"},
    }
    # The existing gate-smoke API still refuses the repository's official path.
    with pytest.raises(RealPilotConfigurationError):
        persist_pair(ROOT / "benchmark" / "results" / "pilot_runs.jsonl", pair)
    assert not official_path.exists()


def test_official_pair_is_validated_in_memory_before_two_record_persistence(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    auth = official._issue_official_authorization()
    with pytest.raises(official.OfficialPilotGateError, match="pair_authorization_invalid"):
        official.persist_official_pair(auth, preflight, {"official_pilot": True}, {})
    assert not preflight.results_path.exists()

    pair = _safe_pair()
    baseline = official._officialize_record(
        pair["baseline"], preflight, timestamp_utc="2026-09-27T00:00:00Z",
        jev_audit=official._baseline_audit(), experimental_validation_cost=None,
    )
    router_audit = {
        "status": "pending_human_review", "reason": "light_rule_audit_only",
        "classifier": None, "experimental_validation_cost": 0.02,
        "assessment": None, "assessment_status": "pending_human_review",
    }
    router = official._officialize_record(
        pair["router"], preflight, timestamp_utc="2026-09-27T00:00:00Z",
        jev_audit=router_audit, experimental_validation_cost=0.02,
    )
    official.persist_official_pair(auth, preflight, baseline, router)
    assert len(preflight.results_path.read_text(encoding="utf-8").splitlines()) == 2


@pytest.mark.parametrize("existing", [b"", b'{"attempt":1,"mode":"baseline","official_pilot":true,"task_id":"task_001"}\n'])
def test_atomic_pair_failure_leaves_existing_state_without_one_new_line(monkeypatch, tmp_path, existing):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    if existing:
        preflight.results_path.write_bytes(existing)
    before = existing
    pair = _safe_pair()
    baseline = official._officialize_record(
        pair["baseline"], preflight, timestamp_utc="2026-09-27T00:00:00Z",
        jev_audit=official._baseline_audit(), experimental_validation_cost=None,
    )
    router = official._officialize_record(
        pair["router"], preflight, timestamp_utc="2026-09-27T00:00:00Z",
        jev_audit={"status": "not_applicable", "assessment": None},
        experimental_validation_cost=None,
    )

    def fail_replace(*_args):
        raise OSError("injected replace failure")

    monkeypatch.setattr(official.os, "replace", fail_replace)
    expected_error = "incomplete_official_pair" if existing else "official_pair_replace_failed"
    with pytest.raises(official.OfficialPilotGateError, match=expected_error):
        official.persist_official_pair(official._issue_official_authorization(), preflight, baseline, router)
    if before:
        assert preflight.results_path.read_bytes() == before
    else:
        assert not preflight.results_path.exists()
    assert list(preflight.results_path.parent.glob(f".{preflight.results_path.name}.*.tmp")) == []


def test_sanitized_records_have_no_prompt_or_secret_like_fields(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    unsafe = _safe_pair()["baseline"]
    unsafe["prompt"] = "must not persist"
    with pytest.raises(official.OfficialPilotGateError, match="unsafe_record"):
        official._officialize_record(
            unsafe, preflight, timestamp_utc="2026-09-27T00:00:00Z",
            jev_audit=official._baseline_audit(), experimental_validation_cost=None,
        )


class _AuditJEV:
    def __init__(self, classifier):
        self.classifier_value = classifier
        self.calls = []
        self.last_call_metrics = {
            "returned_model": "jev-test",
            "request_id": "audit-request",
            "input_tokens": 10,
            "output_tokens": 2,
            "latency_ms": 1,
            "exact_cost": 0.123,
        }

    def classify(self, request):
        self.calls.append(request)
        return self.classifier_value

    def resolve_cost(self, _request, _classifier):
        return CostComponent(cost=0.123, cost_estimated=False, cost_estimation_source="provider_usage")


def _classifier():
    return ClassifierResult(
        task_type="file_operation", difficulty_score=1, difficulty_bucket="low",
        required_capability="low", confidence=0.9, risk_level="low",
    )


def test_light_rule_audit_cost_is_experimental_only(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    audit = _AuditJEV(_classifier())
    audit_data, experimental = official._router_audit(
        {"route_source": "light_rule", "classifier": None, "cost": {"total_production_cost": 0.01}},
        preflight,
        audit_jev_factory=lambda: audit,
    )
    assert len(audit.calls) == 1
    assert experimental == pytest.approx(0.123)
    assert audit_data["experimental_validation_cost"] == pytest.approx(0.123)
    assert audit_data["assessment"] is None


def test_jev_production_audit_reuses_classifier_without_second_call(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    audit_factory_calls = []
    classifier = _classifier().model_dump(mode="json")
    audit_data, experimental = official._router_audit(
        {
            "route_source": "jev", "classifier": classifier,
            "classifier_metrics": {"exact_cost": 0.321},
            "cost": {"classifier_cost": 0.321},
        },
        preflight,
        audit_jev_factory=lambda: audit_factory_calls.append(True),
    )
    assert audit_factory_calls == []
    assert experimental is None
    assert audit_data["classifier"] == classifier
    assert audit_data["assessment"] is None
    assert audit_data["cost"] == pytest.approx(0.321)


def test_task_010_uses_controlled_mock_without_key_and_marks_audit_not_applicable(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_010_fallback", _completed_real_tasks(), key=False, phase="B")
    calls = []
    original = official.run_failure_injection
    monkeypatch.setattr(official, "run_failure_injection", lambda case: (calls.append(case), original(case))[1])
    result = official.execute_official_task(
        "task_010_fallback", official._issue_official_authorization(), preflight
    )
    assert result["records_written"] == 2
    assert len(calls) == 2
    records = [json.loads(line) for line in preflight.results_path.read_text(encoding="utf-8").splitlines()][-2:]
    assert all(record["jev_audit"]["reason"] == "controlled_mock" for record in records)
    assert all(record["jev_audit"]["assessment"] == "not_applicable" for record in records)
    assert all(record["acceptance_status"] == "controlled_mock_passed" for record in records)
    assert all("evidence_artifact" not in record and "manual_review" not in record for record in records)
    assert not (preflight.artifacts_dir / "task_010_fallback").exists()
    with pytest.raises(official.OfficialPilotGateError, match="pilot_complete"):
        official._next_allowed_task(
            _completed_real_tasks() + records,
            preflight.config,
        )


def test_manual_pending_blocks_next_task_and_finalization_unlocks_phase_b(monkeypatch, tmp_path):
    preflight, result = _execute_fake_task1(monkeypatch, tmp_path)
    assert result["records_written"] == 2
    pending = [json.loads(line) for line in preflight.results_path.read_text(encoding="utf-8").splitlines()]
    with pytest.raises(official.OfficialPilotGateError, match="jev_audit_pending"):
        _preflight(monkeypatch, tmp_path, "task_002", phase="B")

    baseline_manifest = tmp_path / pending[0]["evidence_artifact"] / "manifest.json"
    before = baseline_manifest.read_bytes()
    baseline_manifest.write_bytes(before + b"tamper")
    with pytest.raises(official.OfficialPilotGateError, match="manual_artifact_hash_mismatch"):
        official.finalize_manual_acceptance(
            "task_001", official._issue_official_authorization(), True, False,
            fixture_root=FIXTURES, results_path=preflight.results_path,
        )
    baseline_manifest.write_bytes(before)

    final = official.finalize_manual_acceptance(
        "task_001", official._issue_official_authorization(), True, False,
        fixture_root=FIXTURES, results_path=preflight.results_path,
        timestamp_utc="2026-09-30T01:00:00Z",
    )
    assert final["baseline_acceptance_status"] == "manual_passed"
    assert final["router_acceptance_status"] == "manual_failed"
    records = [json.loads(line) for line in preflight.results_path.read_text(encoding="utf-8").splitlines()]
    statuses = {record["mode"]: record["acceptance_status"] for record in records}
    assert statuses == {"baseline": "manual_passed", "router": "manual_failed"}
    assert all(record["manual_review"]["reviewer"] == "advisor" for record in records)
    with pytest.raises(official.OfficialPilotGateError, match="jev_audit_pending"):
        _preflight(monkeypatch, tmp_path, "task_002", phase="B")
    official.finalize_jev_audit_assessment(
        "task_001",
        official._issue_official_authorization(),
        "reasonable",
        results_path=preflight.results_path,
    )
    assert _preflight(monkeypatch, tmp_path, "task_002", phase="B").next_task_id == "task_002"


def test_jev_audit_review_updates_only_router_audit(monkeypatch, tmp_path):
    preflight, _result = _execute_fake_task1(monkeypatch, tmp_path)
    before = [json.loads(line) for line in preflight.results_path.read_text(encoding="utf-8").splitlines()]
    before_by_mode = {record["mode"]: record for record in before}

    final = official.finalize_jev_audit_assessment(
        "task_001",
        official._issue_official_authorization(),
        "reasonable",
        config_path=ROOT / "benchmark" / "pilot_config.yaml",
        results_path=preflight.results_path,
        timestamp_utc="2026-09-30T02:00:00Z",
    )

    assert final == {
        "official_pilot": True,
        "status": "jev_audit_review_finalized",
        "task_id": "task_001",
        "assessment": "reasonable",
    }
    after = [json.loads(line) for line in preflight.results_path.read_text(encoding="utf-8").splitlines()]
    after_by_mode = {record["mode"]: record for record in after}
    assert after_by_mode["baseline"] == before_by_mode["baseline"]
    assert {
        key: value
        for key, value in after_by_mode["router"].items()
        if key != "jev_audit"
    } == {
        key: value
        for key, value in before_by_mode["router"].items()
        if key != "jev_audit"
    }
    expected_audit = dict(before_by_mode["router"]["jev_audit"])
    expected_audit.update(
        {
            "assessment": "reasonable",
            "assessment_status": "reviewed",
            "human_review": {
                "reviewer": "advisor",
                "timestamp_utc": "2026-09-30T02:00:00Z",
            },
        }
    )
    assert after_by_mode["router"]["jev_audit"] == expected_audit


@pytest.mark.parametrize("assessment", ["not_applicable", "bogus"])
def test_jev_audit_review_rejects_invalid_assessment(monkeypatch, tmp_path, assessment):
    preflight, _result = _execute_fake_task1(monkeypatch, tmp_path)
    before = preflight.results_path.read_bytes()
    with pytest.raises(official.OfficialPilotGateError, match="audit_assessment_invalid"):
        official.finalize_jev_audit_assessment(
            "task_001",
            official._issue_official_authorization(),
            assessment,
            results_path=preflight.results_path,
        )
    assert preflight.results_path.read_bytes() == before


def test_jev_audit_review_rejects_missing_classifier_and_double_review(monkeypatch, tmp_path):
    preflight, _result = _execute_fake_task1(monkeypatch, tmp_path)
    records = [json.loads(line) for line in preflight.results_path.read_text(encoding="utf-8").splitlines()]
    router = next(record for record in records if record["mode"] == "router")
    router["jev_audit"]["classifier"] = None
    preflight.results_path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    with pytest.raises(official.OfficialPilotGateError, match="jev_audit_review_not_pending"):
        official.finalize_jev_audit_assessment(
            "task_001",
            official._issue_official_authorization(),
            "reasonable",
            results_path=preflight.results_path,
        )

    preflight, _result = _execute_fake_task1(monkeypatch, tmp_path / "double")
    official.finalize_jev_audit_assessment(
        "task_001",
        official._issue_official_authorization(),
        "questionable",
        results_path=preflight.results_path,
    )
    before = preflight.results_path.read_bytes()
    with pytest.raises(official.OfficialPilotGateError, match="jev_audit_review_not_pending"):
        official.finalize_jev_audit_assessment(
            "task_001",
            official._issue_official_authorization(),
            "reasonable",
            results_path=preflight.results_path,
        )
    assert preflight.results_path.read_bytes() == before


def test_pending_jev_audit_blocks_next_task_until_reviewed(monkeypatch, tmp_path):
    pending_pair = [
        dict(
            _record("task_001", mode),
            acceptance_status="passed",
            jev_audit={
                "status": "pending_human_review",
                "assessment_status": "pending_human_review",
                "classifier": {"difficulty_score": 1},
            },
        )
        for mode in ("baseline", "router")
    ]
    pending_pair[0]["jev_audit"] = {
        "status": "not_applicable",
        "assessment_status": "not_applicable",
        "assessment": None,
    }
    with pytest.raises(official.OfficialPilotGateError, match="jev_audit_pending"):
        _preflight(monkeypatch, tmp_path, "task_002", pending_pair, phase="B")

    reviewed_pair = [dict(record) for record in pending_pair]
    reviewed_pair[1]["jev_audit"] = {
        "status": "reviewed",
        "assessment_status": "reviewed",
        "assessment": "reasonable",
        "classifier": {"difficulty_score": 1},
    }
    assert _preflight(monkeypatch, tmp_path / "reviewed", "task_002", reviewed_pair, phase="B").next_task_id == "task_002"


def test_audit_review_cli_requires_exact_confirmation_and_assessment(monkeypatch, capsys):
    assert official_script.main(["--task", "task_001", "--audit-review"]) == 2
    assert json.loads(capsys.readouterr().out)["error_codes"] == ["jev_audit_review_confirmation_required"]

    assert official_script.main([
        "--task", "task_001",
        "--audit-review",
        "--confirm", "OFFICIAL_PILOT_JEV_REVIEW",
    ]) == 2
    assert json.loads(capsys.readouterr().out)["error_codes"] == ["jev_assessment_required"]

    calls = []
    monkeypatch.setattr(official_script, "_issue_official_authorization", lambda: "auth")
    monkeypatch.setattr(
        official_script,
        "finalize_jev_audit_assessment",
        lambda task, authorization, assessment: calls.append((task, authorization, assessment)) or {"ok": True},
    )
    assert official_script.main([
        "--task", "task_001",
        "--audit-review",
        "--confirm", "OFFICIAL_PILOT_JEV_REVIEW",
        "--jev-assessment", "reasonable",
    ]) == 0
    assert calls == [("task_001", "auth", "reasonable")]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_automated_pair_does_not_require_manual_finalize(monkeypatch, tmp_path):
    first_pair = [dict(_record("task_001", mode), acceptance_status="passed") for mode in ("baseline", "router")]
    assert _preflight(monkeypatch, tmp_path, "task_002", first_pair, phase="B").next_task_id == "task_002"


def test_artifact_bundle_has_response_refs_and_no_evidence_body_in_jsonl(monkeypatch, tmp_path):
    preflight, _result = _execute_fake_task1(monkeypatch, tmp_path)
    records = [json.loads(line) for line in preflight.results_path.read_text(encoding="utf-8").splitlines()]
    assert all(record["evidence_artifact"].startswith("benchmark/results/artifacts/task_001") for record in records)
    assert all(len(record["evidence_manifest_sha256"]) == 64 for record in records)
    result_text = preflight.results_path.read_text(encoding="utf-8")
    assert "unique-official-answer" not in result_text
    for record in records:
        mode_dir = tmp_path / record["evidence_artifact"]
        assert (mode_dir / "response.txt").read_text(encoding="utf-8") == "unique-official-answer"
        assert (mode_dir / "manifest.json").is_file()
        assert not (mode_dir / "workspace").exists()


def test_artifact_manifest_captures_changed_and_deleted_allowed_text(tmp_path, monkeypatch):
    first_pair = [dict(_record("task_001", mode), acceptance_status="passed") for mode in ("baseline", "router")]
    preflight = _preflight(monkeypatch, tmp_path, "task_002", first_pair, phase="B")
    initial = {
        "README.md": {"status": "present", "kind": "file", "sha256": "before-readme", "text": "before"},
        "removed.txt": {"status": "present", "kind": "file", "sha256": "before-removed", "text": "gone"},
    }
    final = {"README.md": {
        "status": "present", "kind": "file",
        "sha256": official.hashlib.sha256(b"after").hexdigest(), "text": "after",
    }}
    plan, _size = official._build_evidence_plan(
        preflight, ("README.md", "removed.txt"), initial, "baseline",
        {"response_text": "answer", "allowed_paths": final, "error": None},
    )
    manifest = plan[2]
    assert manifest["allowed_paths"]["changed"] == [
        {"path": "README.md", "sha256": official.hashlib.sha256(b"after").hexdigest()}
    ]
    assert manifest["allowed_paths"]["deleted"] == [{"path": "removed.txt", "sha256": "before-removed"}]
    assert plan[1]["README.md"] == b"after"


def test_artifact_rename_failure_leaves_no_final_bundle(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    monkeypatch.setattr(official.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("rename failure")))
    with pytest.raises(official.OfficialPilotGateError, match="evidence_artifact_replace_failed"):
        official._materialize_evidence_bundle(preflight, _evidence())
    assert not (preflight.artifacts_dir / "task_001").exists()
    assert list(preflight.artifacts_dir.glob(".task_001.*")) == []


def test_jsonl_failure_removes_new_artifact_bundle(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    pair = _safe_pair()
    pair["_evidence"] = _evidence()
    monkeypatch.setattr(official, "run_real_pilot_pair", lambda *args, **kwargs: pair)
    monkeypatch.setattr(
        official, "persist_official_pair",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            official.OfficialPilotGateError("injected", error_code="jsonl_failure")
        ),
    )
    with pytest.raises(official.OfficialPilotGateError, match="jsonl_failure"):
        official.execute_official_task(
            "task_001", official._issue_official_authorization(), preflight,
            audit_jev_factory=lambda: _AuditJEV(_classifier()),
        )
    assert not (preflight.artifacts_dir / "task_001").exists()


def test_evidence_secret_filter_is_precise_and_rejects_key_path(monkeypatch, tmp_path):
    key_path = tmp_path / "jev-key"
    monkeypatch.setenv("JEV_API_KEY_FILE", str(key_path))
    harmless = "source identifier api_key is documented here"
    assert official._sanitize_artifact_text(harmless) == harmless
    sanitized = official._sanitize_artifact_text(
        f"Bearer abc.def token sk-abcdefgh {key_path} api_key='secret-value'"
    )
    assert "abc.def" not in sanitized
    assert "sk-abcdefgh" not in sanitized
    assert str(key_path) not in sanitized
    assert "secret-value" not in sanitized
    for assignment, secret in (
        ("api_key=SECRET123", "SECRET123"),
        ("password=hunter2", "hunter2"),
        ("access_token: COLONVALUE", "COLONVALUE"),
        ("secret: 'quoted-value'", "quoted-value"),
    ):
        assert secret not in official._sanitize_artifact_text(assignment)

    source_like = 'message="Authorization: Bearer token-secret; secret=private-value",'
    sanitized_like = official._sanitize_artifact_text(source_like)
    assert sanitized_like.startswith('message="') and sanitized_like.endswith('",')
    assert "token-secret" not in sanitized_like
    assert "private-value" not in sanitized_like
    quoted_source = '"api_key=api-key-secret-123456"'
    sanitized_quoted = official._sanitize_artifact_text(quoted_source)
    assert sanitized_quoted.startswith('"') and sanitized_quoted.endswith('"')
    assert "api-key-secret-123456" not in sanitized_quoted

    source = "\n".join(
        (
            'message="Authorization: Bearer token-secret; secret=private-value",',
            'api_value = "api_key=api-key-secret-123456"',
            'password_value = "password=hunter2"',
            'colon_value = "password: hunter2"',
            'quoted_value = "secret=\'quoted-value\'"',
            'harmless = "source identifier api_key is harmless"',
        )
    )
    sanitized_source = official._sanitize_artifact_text(source)
    compile(sanitized_source, "<artifact>", "exec")
    for secret in (
        "token-secret",
        "private-value",
        "api-key-secret-123456",
        "hunter2",
        "quoted-value",
    ):
        assert secret not in sanitized_source
    assert "source identifier api_key is harmless" in sanitized_source


def test_manual_artifact_content_verification_rejects_tampered_response_and_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(official, "PROJECT_ROOT", tmp_path)
    artifacts_dir = tmp_path / "benchmark" / "results" / "artifacts"
    mode_dir = artifacts_dir / "task_001" / "attempt_1" / "baseline"
    workspace_dir = mode_dir / "workspace"
    workspace_dir.mkdir(parents=True)
    response_path = mode_dir / "response.txt"
    workspace_path = workspace_dir / "README.md"
    response_path.write_bytes(b"answer")
    workspace_path.write_bytes(b"changed")
    manifest = {
        "schema_version": "0.1",
        "task_id": "task_001",
        "attempt": 1,
        "mode": "baseline",
        "response_sha256": official.hashlib.sha256(b"answer").hexdigest(),
        "allowed_paths": {
            "changed": [{"path": "README.md", "sha256": official.hashlib.sha256(b"changed").hexdigest()}],
            "new": [],
            "deleted": [],
        },
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    (mode_dir / "manifest.json").write_bytes(manifest_bytes)
    record = {
        "task_id": "task_001",
        "mode": "baseline",
        "attempt": 1,
        "evidence_artifact": "benchmark/results/artifacts/task_001/attempt_1/baseline",
        "evidence_manifest_sha256": official.hashlib.sha256(manifest_bytes).hexdigest(),
    }

    official._verify_artifact_reference(record, artifacts_dir, "task_001", ("README.md",))
    response_path.write_bytes(b"tampered response")
    with pytest.raises(official.OfficialPilotGateError, match="manual_artifact_content_mismatch"):
        official._verify_artifact_reference(record, artifacts_dir, "task_001", ("README.md",))
    response_path.write_bytes(b"answer")
    workspace_path.write_bytes(b"tampered workspace")
    with pytest.raises(official.OfficialPilotGateError, match="manual_artifact_content_mismatch"):
        official._verify_artifact_reference(record, artifacts_dir, "task_001", ("README.md",))


def test_successful_empty_response_is_rejected_but_pre_provider_failure_is_allowed(monkeypatch, tmp_path):
    preflight = _preflight(monkeypatch, tmp_path, "task_001")
    evidence = {"response_text": "", "allowed_paths": {}, "error": None}
    with pytest.raises(official.OfficialPilotGateError, match="evidence_response_missing"):
        official._build_evidence_plan(
            preflight, (), {}, "baseline", evidence, {"route_status": "success"}
        )
    plan, _size = official._build_evidence_plan(
        preflight, (), {}, "baseline", evidence, {"route_status": "failed"}
    )
    assert plan[0] == ""


def test_allowed_path_capture_rejects_escape_and_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "ok.txt").write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)
    with pytest.raises(RealPilotConfigurationError, match="safe relative path"):
        official._capture_allowed_path_state(workspace, ("../outside.txt",))
    with pytest.raises(RealPilotConfigurationError, match="symlink|escapes workspace"):
        official._capture_allowed_path_state(workspace, ("link.txt",))


def test_malformed_jsonl_stops_and_attempt_two_is_blocked(monkeypatch, tmp_path):
    monkeypatch.setattr(official, "PROJECT_ROOT", tmp_path)
    result_path = tmp_path / "benchmark" / "results" / "pilot_runs.jsonl"
    result_path.parent.mkdir(parents=True)
    result_path.write_text('{"official_pilot":true', encoding="utf-8")
    with pytest.raises(official.OfficialPilotGateError, match="official_results_malformed"):
        _preflight(monkeypatch, tmp_path, "task_001")

    result_path.write_text(json.dumps(_record("task_001", "baseline", attempt=2)) + "\n", encoding="utf-8")
    with pytest.raises(official.OfficialPilotGateError, match="repeat_attempt_forbidden"):
        _preflight(monkeypatch, tmp_path, "task_001")
