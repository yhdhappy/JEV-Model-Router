"""T-10 minimal JSONL logging tests."""

import inspect
import json
import os
import stat

import pytest

from jev_router.logging_store import JsonlLoggingStore
from jev_router.providers import MockProvider, ModelResponse
from jev_router.registry import ModelDefinition, ModelRegistry
from jev_router.router import Router
from jev_router.schemas import (
    ClassifierResult,
    CostBreakdown,
    RouteError,
    RouteRequest,
    RouteResult,
)


def classifier(*, notes=None):
    return ClassifierResult(
        task_type="coding",
        difficulty_score=7,
        difficulty_bucket="high",
        required_capability="high",
        confidence=0.91,
        risk_level="medium",
        notes=notes,
    )


def result(**overrides):
    payload = {
        "task_id": "task-10",
        "status": "success",
        "route_source": "jev",
        "rule_id": None,
        "classifier": classifier(notes="private classifier note"),
        "selected_model": "high_model",
        "fallback_history": [],
        "cost": CostBreakdown(
            classifier_cost=0.001,
            execution_cost=0.02,
            fallback_cost=0.0,
            total_production_cost=0.021,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        ),
        "acceptance": None,
        "errors": [],
    }
    payload.update(overrides)
    return RouteResult(**payload)


def read_records(path):
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return lines, [json.loads(line) for line in lines]


def test_one_result_is_one_jsonl_line_with_required_safe_fields(tmp_path):
    path = tmp_path / "nested" / "runs.jsonl"
    store = JsonlLoggingStore(path)

    store.append(result())

    lines, records = read_records(path)
    record = records[0]
    assert len(lines) == 1
    assert lines[0].endswith("\n")
    assert record == {
        "schema_version": "0.1",
        "task_id": "task-10",
        "status": "success",
        "route_source": "jev",
        "rule_id": None,
        "classifier": {
            "task_type": "coding",
            "difficulty_score": 7,
            "difficulty_bucket": "high",
            "required_capability": "high",
            "confidence": 0.91,
            "risk_level": "medium",
        },
        "selected_model": "high_model",
        "jev_called": True,
        "fallback_used": False,
        "fallback_history": [],
        "classifier_cost": 0.001,
        "execution_cost": 0.02,
        "fallback_cost": 0.0,
        "total_production_cost": 0.021,
        "cost_estimated": False,
        "cost_estimation_source": "provider_usage",
        "error_codes": [],
    }


def test_append_preserves_first_record_and_adds_second_valid_line(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)

    store(result(task_id="first"))
    first_line = path.read_text(encoding="utf-8")
    store.append(result(task_id="second"))

    lines, records = read_records(path)
    assert len(lines) == 2
    assert lines[0] == first_line
    assert [record["task_id"] for record in records] == ["first", "second"]


def test_fallback_history_and_fallback_cost_are_traceable(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    fallback = result(
        route_source="fallback",
        selected_model="fallback_model",
        fallback_history=["primary_model:provider_timeout", "fallback_model:success"],
        cost=CostBreakdown(
            classifier_cost=0.001,
            execution_cost=0.0,
            fallback_cost=0.006,
            total_production_cost=0.007,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        ),
    )

    store.append(fallback)

    record = read_records(path)[1][0]
    assert record["fallback_used"] is True
    assert record["fallback_history"] == [
        "primary_model:provider_timeout",
        "fallback_model:success",
    ]
    assert record["fallback_cost"] == 0.006


def test_estimated_cost_fields_remain_traceable(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    store.append(
        result(
            cost=CostBreakdown(
                classifier_cost=0.001,
                execution_cost=0.02,
                fallback_cost=0.0,
                total_production_cost=0.021,
                cost_estimated=True,
                cost_estimation_source="heuristic",
            )
        )
    )

    record = read_records(path)[1][0]
    assert record["cost_estimated"] is True
    assert record["cost_estimation_source"] == "heuristic"
    assert record["total_production_cost"] == 0.021


def test_classifier_safe_subset_is_present_and_notes_are_absent(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    store.append(result())

    record = read_records(path)[1][0]
    assert set(record["classifier"]) == {
        "task_type",
        "difficulty_score",
        "difficulty_bucket",
        "required_capability",
        "confidence",
        "risk_level",
    }
    assert "notes" not in record["classifier"]
    assert "private classifier note" not in path.read_text(encoding="utf-8")


def test_acceptance_prompt_and_api_key_are_not_logged(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    store.append(
        result(
            acceptance={
                "prompt": "private customer conversation",
                "api_key": "sk-live-acceptance-secret-123456",
            }
        )
    )

    text = path.read_text(encoding="utf-8")
    assert "private customer conversation" not in text
    assert "sk-live-acceptance-secret-123456" not in text
    assert "acceptance" not in text


def test_error_messages_are_excluded_and_only_codes_are_logged(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    store.append(
        result(
            status="failed",
            errors=[
                RouteError(
                    code="provider_error",
                    message="Authorization: Bearer token-secret; secret=private-value",
                )
            ],
        )
    )

    record = read_records(path)[1][0]
    text = path.read_text(encoding="utf-8")
    assert record["error_codes"] == ["provider_error"]
    assert "Authorization" not in text
    assert "Bearer" not in text
    assert "token-secret" not in text
    assert "private-value" not in text


def test_sanitizer_redacts_secret_looking_identifiers_and_history(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    secrets = [
        "sk-live-model-secret-123456",
        "api_key=api-key-secret-123456",
        "Bearer bearer-secret-123456",
        "secret=fallback-secret-123456",
    ]
    store.append(
        result(
            task_id="task-" + secrets[0],
            selected_model="model-" + secrets[1],
            fallback_history=secrets[2:] + [secrets[3]],
        )
    )

    text = path.read_text(encoding="utf-8")
    assert all(secret not in text for secret in secrets)
    assert text.count("[REDACTED]") >= len(secrets)


def test_newline_injection_stays_on_one_physical_line(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    store.append(
        result(
            task_id="task-before\nforged-record\r\nnext",
            selected_model="model\nforged",
            fallback_history=["history\r\nforged"],
        )
    )

    lines, records = read_records(path)
    assert len(lines) == 1
    assert len(records) == 1
    assert records[0]["task_id"] == "task-before\nforged-record\r\nnext"


def test_store_as_router_sink_writes_one_terminal_record(tmp_path):
    path = tmp_path / "runs.jsonl"
    store = JsonlLoggingStore(path)
    provider = MockProvider(
        [
            ModelResponse(
                text="answer",
                input_tokens=100,
                output_tokens=50,
                provider_request_id="request-id",
            )
        ]
    )
    router = Router(
        ModelRegistry(
            {
                "model": ModelDefinition(
                    provider="test-provider",
                    model_id="actual-model",
                    capability="low",
                    task_types=["file_operation"],
                    input_cost_per_million=1,
                    output_cost_per_million=1,
                    enabled=True,
                )
            }
        ),
        {"model": provider},
        lambda request: classifier(),
        safe_default_model="model",
        estimated_max_costs={"model": 0.1},
        result_sink=store,
    )

    route_result = router.route(
        RouteRequest(task_id="router-task", prompt="read README.md")
    )

    lines, records = read_records(path)
    assert route_result.status == "success"
    assert len(lines) == 1
    assert records[0]["task_id"] == "router-task"


def test_parent_directories_append_permissions_and_invalid_input(tmp_path):
    path = tmp_path / "a" / "b" / "runs.jsonl"
    store = JsonlLoggingStore(path)
    store.append(result())
    assert path.parent.is_dir()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    os.chmod(path, 0o640)
    JsonlLoggingStore(path).append(result(task_id="existing"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o640
    assert len(read_records(path)[0]) == 2

    with pytest.raises(TypeError):
        store.append("not a RouteResult")


def test_t10_store_has_no_cli_ui_network_or_credential_surface():
    text = inspect.getsource(JsonlLoggingStore)
    assert "requests" not in text
    assert "urllib" not in text
    assert "keychain" not in text.lower()
