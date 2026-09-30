"""Offline tests for the frozen Pilot Summary gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import benchmark.pilot_summary as summary_module
from benchmark.pilot_summary import (
    PilotSummaryError,
    build_pilot_summary,
    finalize_pilot_summary,
    render_pilot_summary_markdown,
)


ROOT = Path(__file__).parents[1]
REAL_TASKS = [f"task_{number:03d}" for number in range(1, 10)]
CONFIG = ROOT / "benchmark" / "pilot_config.yaml"
REGISTRY = ROOT / "config" / "models.real-pilot.yaml"


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "benchmark" / "pilot_config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _cost(total: float, *, estimated: bool = False, fallback: float = 0.0) -> dict:
    return {
        "classifier_cost": 0.0,
        "execution_cost": total - fallback,
        "fallback_cost": fallback,
        "total_production_cost": total,
        "cost_estimated": estimated,
    }


def _audit(mode: str, assessment: str = "reasonable", *, classifier: dict | None = None) -> dict:
    if mode == "baseline":
        return {
            "status": "not_applicable",
            "assessment_status": "not_applicable",
            "assessment": None,
            "classifier": None,
        }
    return {
        "status": "pending_human_review" if assessment != "not_applicable" else "not_applicable",
        "assessment_status": "reviewed" if assessment != "not_applicable" else "not_applicable",
        "assessment": assessment if assessment != "not_applicable" else "not_applicable",
        "classifier": classifier,
    }


def _record(task: str, mode: str, attempt: int = 1, *, total: float, passed: bool = True, estimated: bool = False,
            assessment: str = "reasonable", capability: str = "low", selected_model: str = "low_model",
            validation_cost: float | None = None, controlled_case: str | None = None,
            fallback_cost: float = 0.0) -> dict:
    controlled = task == "task_010_fallback"
    audit = _audit(
        mode,
        "not_applicable" if controlled else assessment,
        classifier=None if controlled or mode == "baseline" else {
            "required_capability": capability,
            "difficulty_score": 1 if capability == "low" else 8,
            "confidence": 0.8,
        },
    )
    if controlled:
        audit["assessment"] = "not_applicable"
    record = {
        "official_pilot": True,
        "config_schema_version": "0.1",
        "task_id": task,
        "mode": mode,
        "attempt": attempt,
        "acceptance_status": "controlled_mock_passed" if controlled else ("passed" if passed else "manual_failed"),
        "cost": _cost(total, estimated=estimated, fallback=fallback_cost),
        "error_codes": [] if passed else ["acceptance_failed"],
        "jev_audit": audit,
        "selected_model": selected_model if not controlled else None,
        "route_status": "success" if passed else "failed",
        "route_source": "fallback" if controlled_case == "case_a_fallback_success" else "jev",
    }
    if validation_cost is not None:
        record["experimental_validation_cost"] = validation_cost
    if controlled:
        record["controlled_mock_case"] = controlled_case
        record["controlled_mock_summary"] = {
            "case": controlled_case,
            "provider_call_counts": (
                {"primary": 1, "fallback_1": 1}
                if controlled_case == "case_a_fallback_success"
                else {"primary": 1, "fallback_1": 0, "fallback_2": 0}
            ),
        }
        if controlled_case == "case_b_budget_block":
            record["route_status"] = "failed"
            record["error_codes"] = ["provider_timeout", "budget_limit_reached"]
    if attempt > 1:
        record["repeat_authorization"] = {"parent_attempt": attempt - 1, "authorized_by": "advisor"}
    return record


def _write_records(tmp_path: Path, records: list[dict]) -> Path:
    config_path = _config(tmp_path)
    result_path = tmp_path / "benchmark" / "results" / "pilot_runs.jsonl"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")
    return config_path


def _fixture_records(*, repeats: bool = True) -> list[dict]:
    records = []
    for index, task in enumerate(REAL_TASKS, 1):
        records.extend(
            [
                _record(task, "baseline", total=float(index), estimated=index == 1, passed=True, selected_model="high_model"),
                _record(
                    task,
                    "router",
                    total=float(index) + 0.5,
                    estimated=index == 2,
                    passed=index != 2,
                    assessment="questionable" if index == 2 else ("clearly_unreasonable" if index == 3 else ("not_applicable" if index == 4 else "reasonable")),
                    capability="high" if index == 3 else "low",
                    selected_model="medium_model",
                    validation_cost=0.01 if index == 1 else None,
                ),
            ]
        )
        if repeats and task == "task_003":
            records.extend(
                [
                    _record(task, "baseline", attempt=2, total=30.0, passed=True),
                    _record(task, "router", attempt=2, total=31.0, passed=False, assessment="reasonable", validation_cost=0.02),
                    _record(task, "baseline", attempt=3, total=40.0, passed=True),
                    _record(task, "router", attempt=3, total=41.0, passed=True, assessment="questionable", validation_cost=0.03),
                ]
            )
    records.extend(
        [
            _record("task_010_fallback", "baseline", total=0.013, controlled_case="case_a_fallback_success", fallback_cost=0.003),
            _record("task_010_fallback", "router", total=0.04, controlled_case="case_b_budget_block"),
        ]
    )
    return records


def _build(tmp_path: Path, records: list[dict] | None = None) -> dict:
    config_path = _write_records(tmp_path, records or _fixture_records())
    return build_pilot_summary(config_path=config_path, model_registry_path=REGISTRY)


def test_first_attempt_totals_exclude_repeats_and_task010_and_repeat_cost_is_separate(tmp_path):
    result = _build(tmp_path)
    assert result["total_tasks"] == 10
    assert result["real_comparison_tasks"] == 9
    assert result["baseline_total_cost"] == pytest.approx(sum(range(1, 10)))
    assert result["router_total_cost"] == pytest.approx(sum(index + 0.5 for index in range(1, 10)))
    assert result["repeat_attempt_pairs"] == 2
    assert result["repeat_run_cost"] == pytest.approx(30 + 31 + 40 + 41)
    assert result["repeat_tasks"] == ["task_003"]
    assert result["repeat_evidence"]["task_003"]["attempt_numbers"] == [2, 3]
    assert result["fallback_tests_passed"] is True


def test_failed_router_classifier_only_spend_is_not_cost_comparable_or_router_lower(tmp_path):
    records = _fixture_records()
    router = next(record for record in records if record["task_id"] == "task_002" and record["mode"] == "router")
    router["cost"]["total_production_cost"] = 0.001
    router["route_status"] = "failed"
    result = _build(tmp_path, records)
    assert "task_002" not in result["cost_comparable_tasks"]
    assert "task_002" not in result["tasks_where_router_cost_lower"]
    assert set(result["tasks_where_router_cost_lower"]).issubset(set(result["cost_comparable_tasks"]))


def test_jev_counts_are_first_attempt_only_and_task010_is_not_applicable(tmp_path):
    result = _build(tmp_path)
    assert result["jev_reasonable"] == 6
    assert result["jev_questionable"] == 1
    assert result["jev_clearly_unreasonable"] == 1
    assert result["jev_not_applicable"] == 1
    assert result["repeat_evidence"]["task_003"]["jev_assessment_counts"] == {"questionable": 1, "reasonable": 1}


def test_cost_credibility_keeps_estimated_and_exact_counts_separate(tmp_path):
    result = _build(tmp_path)
    assert result["cost_estimated_runs"] == 2
    assert result["exact_cost_runs"] == 16
    assert result["repeat_cost_estimated_runs"] == 0
    assert result["repeat_exact_cost_runs"] == 4
    assert result["experimental_validation_cost"] == pytest.approx(0.06)


@pytest.mark.parametrize("mutation", ["pending_acceptance", "pending_audit", "duplicate", "gap", "unknown", "over_max"])
def test_validation_rejects_unresolved_duplicate_gap_unknown_and_over_max(tmp_path, mutation):
    records = _fixture_records()
    if mutation == "pending_acceptance":
        records[0]["acceptance_status"] = "pending_manual"
    elif mutation == "pending_audit":
        next(record for record in records if record["task_id"] == "task_001" and record["mode"] == "router")["jev_audit"]["assessment_status"] = "pending_human_review"
    elif mutation == "duplicate":
        records.append(dict(records[0]))
    elif mutation == "gap":
        records = [record for record in records if not (record["task_id"] == "task_003" and record["attempt"] == 2)]
    elif mutation == "unknown":
        records.append(_record("task_011", "baseline", total=1.0))
    elif mutation == "over_max":
        records.extend([
            _record("task_003", "baseline", attempt=4, total=50.0),
            _record("task_003", "router", attempt=4, total=51.0),
        ])
    with pytest.raises(PilotSummaryError):
        _build(tmp_path, records)


@pytest.mark.parametrize("case,field,value", [
    ("case_a_fallback_success", "route_source", "jev"),
    ("case_a_fallback_success", "fallback_cost", 0.0),
    ("case_b_budget_block", "error_codes", []),
])
def test_controlled_fallback_contract_rejects_variants(tmp_path, case, field, value):
    records = _fixture_records()
    record = next(record for record in records if record.get("controlled_mock_case") == case)
    if field == "fallback_cost":
        record["cost"][field] = value
    else:
        record[field] = value
    with pytest.raises(PilotSummaryError):
        _build(tmp_path, records)


def test_check_summary_contains_no_raw_provider_or_prompt_material(tmp_path):
    result = _build(tmp_path)
    encoded = json.dumps(result, ensure_ascii=False).lower()
    for forbidden in ("prompt", "raw provider", "authorization", "credential", "provider response"):
        assert forbidden not in encoded


def test_cli_check_prints_decision_free_json_without_writing_outputs(tmp_path, capsys):
    config_path = _write_records(tmp_path, _fixture_records())
    from scripts import pilot_summary as cli

    assert cli.main(["--check", "--config", str(config_path), "--model-registry", str(REGISTRY)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] is None
    assert output["decision_effectiveness"] is None
    assert not (tmp_path / "benchmark" / "results" / "pilot_summary.json").exists()
    assert not (tmp_path / "benchmark" / "results" / "pilot_summary.md").exists()


def test_finalization_requires_explicit_decision_and_effectiveness(tmp_path, capsys):
    config_path = _write_records(tmp_path, _fixture_records())
    from scripts import pilot_summary as cli

    assert cli.main(["--finalize", "--config", str(config_path), "--decision-notes", "review"]) == 2
    assert "requires decision" in capsys.readouterr().err
    assert cli.main([
        "--check", "--config", str(config_path), "--decision", "go"
    ]) == 2
    assert "check mode" in capsys.readouterr().err


def test_finalization_writes_both_frozen_outputs_without_touching_inputs(tmp_path):
    config_path = _write_records(tmp_path, _fixture_records())
    config_before = config_path.read_bytes()
    result_path = tmp_path / "benchmark" / "results" / "pilot_runs.jsonl"
    result_before = result_path.read_bytes()
    result = finalize_pilot_summary(
        decision="adjust",
        decision_effectiveness="mixed",
        decision_notes="Review cost and quality evidence before integration.",
        config_path=config_path,
        model_registry_path=REGISTRY,
    )
    assert result["decision"] == "adjust"
    assert json.loads((tmp_path / "benchmark" / "results" / "pilot_summary.json").read_text()) == result
    markdown = (tmp_path / "benchmark" / "results" / "pilot_summary.md").read_text()
    for heading in ("## 结论", "## 任务", "## 成本", "## Fallback", "## 是否进入真实 Agent 集成"):
        assert heading in markdown
    assert "否：先调整受影响模块并重跑受影响任务。" in markdown
    assert config_path.read_bytes() == config_before
    assert result_path.read_bytes() == result_before


def test_results_override_must_match_frozen_config(tmp_path):
    config_path = _write_records(tmp_path, _fixture_records())
    with pytest.raises(PilotSummaryError, match="does not match frozen config"):
        build_pilot_summary(config_path=config_path, results_path=tmp_path / "other.json", model_registry_path=REGISTRY)


@pytest.mark.parametrize(
    ("decision", "expected"),
    [
        ("go", "是：进入真实 Agent 集成验证。"),
        ("adjust", "否：先调整受影响模块并重跑受影响任务。"),
        ("stop", "否：暂停扩展，不进入 Adapter/UI。"),
        (None, "未设置（等待 advisor）。"),
    ],
)
def test_markdown_maps_only_explicit_advisor_decision_to_integration_text(tmp_path, decision, expected):
    summary = _build(tmp_path)
    summary["decision"] = decision
    markdown = render_pilot_summary_markdown(summary)
    assert expected in markdown


def test_second_replacement_failure_rolls_back_both_outputs(tmp_path, monkeypatch):
    config_path = _write_records(tmp_path, _fixture_records())
    json_path = tmp_path / "benchmark" / "results" / "pilot_summary.json"
    markdown_path = tmp_path / "benchmark" / "results" / "pilot_summary.md"
    json_path.write_bytes(b"old-json\n")
    markdown_path.write_bytes(b"old-markdown\n")
    original_replace = summary_module.os.replace

    def fail_markdown(source, target):
        if Path(target) == markdown_path:
            raise OSError("simulated second replacement failure")
        return original_replace(source, target)

    monkeypatch.setattr(summary_module.os, "replace", fail_markdown)
    with pytest.raises(PilotSummaryError, match="rolled back"):
        finalize_pilot_summary(
            decision="go",
            decision_effectiveness="supports",
            decision_notes="Advisor review complete.",
            config_path=config_path,
            model_registry_path=REGISTRY,
        )
    assert json_path.read_bytes() == b"old-json\n"
    assert markdown_path.read_bytes() == b"old-markdown\n"
