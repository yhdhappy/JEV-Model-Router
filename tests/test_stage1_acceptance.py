"""T-15 Stage 1 acceptance smoke tests.

These tests intentionally cover one observable end-to-end contract per smoke
case.  They use the real Router, registry, cost, logging, and fixture code,
with deterministic local providers only.
"""

from __future__ import annotations

import json
from pathlib import Path

from benchmark.failure_injection import FailureInjectionCase, run_failure_injection
from benchmark.runner import run_fixture
from jev_router.cost import (
    CostComponent,
    TokenUsage,
    calculate_cost_breakdown,
    calculate_cost_from_usage,
)
from jev_router.logging_store import JsonlLoggingStore
from jev_router.providers import MockProvider, MockScenario, ModelResponse
from jev_router.registry import ModelDefinition, ModelRegistry
from jev_router.router import Router
from jev_router.schemas import (
    ClassifierResult,
    RouteError,
    RouteRequest,
    RouteResult,
    TaskType,
)


PROJECT_ROOT = Path(__file__).parents[1]


def _model(
    name: str,
    *,
    capability: str,
    task_types=(TaskType.CODING,),
    price: int = 1,
) -> ModelDefinition:
    return ModelDefinition(
        provider="mock",
        model_id=f"actual-{name}",
        capability=capability,
        task_types=list(task_types),
        input_cost_per_million=price,
        output_cost_per_million=price,
        enabled=True,
    )


def _classifier(*, capability: str = "medium") -> ClassifierResult:
    return ClassifierResult(
        task_type=TaskType.CODING,
        difficulty_score=5,
        difficulty_bucket="medium",
        required_capability=capability,
        confidence=0.88,
        risk_level="medium",
    )


def _response(*, text: str = "mock response") -> ModelResponse:
    return ModelResponse(
        text=text,
        input_tokens=100,
        output_tokens=50,
        provider_request_id="stage1-smoke-request",
    )


def _exact_cost(value: float) -> CostComponent:
    return CostComponent(
        cost=value,
        cost_estimated=False,
        cost_estimation_source="provider_usage",
    )


def test_stage1_config_loads_through_model_registry():
    registry = ModelRegistry.from_yaml(PROJECT_ROOT / "config" / "models.yaml")

    assert registry.names() == ("low_model", "medium_model", "high_model")
    assert registry.enabled_names() == registry.names()


def test_stage1_light_rule_skips_jev_and_invokes_selected_provider():
    registry = ModelRegistry.from_yaml(PROJECT_ROOT / "config" / "models.yaml")
    provider = MockProvider([_response()])
    jev_calls = []

    def unexpected_jev(request):
        jev_calls.append(request)
        raise AssertionError("light rule must not invoke JEV")

    router = Router(
        registry,
        {"low_model": provider},
        unexpected_jev,
        safe_default_model="low_model",
        estimated_max_costs={"low_model": 0.10},
    )

    result = router.route(
        RouteRequest(task_id="stage1-light", prompt="read README.md")
    )

    assert result.status == "success"
    assert result.route_source == "light_rule"
    assert result.rule_id == "simple_file_read"
    assert result.selected_model == "low_model"
    assert result.classifier is None
    assert result.possible_rule_misclassification is False
    assert jev_calls == []
    assert provider.call_count == 1


def test_stage1_light_rule_failure_marks_possible_misclassification():
    provider = MockProvider([MockScenario.UNAVAILABLE])
    router = Router(
        ModelRegistry(
            {
                "low_model": _model(
                    "low_model",
                    capability="low",
                    task_types=(TaskType.FILE_OPERATION,),
                )
            }
        ),
        {"low_model": provider},
        lambda request: (_ for _ in ()).throw(
            AssertionError("light rule must not invoke JEV")
        ),
        safe_default_model="low_model",
    )

    result = router.route(
        RouteRequest(task_id="stage1-light-failure", prompt="read README.md")
    )

    assert result.status == "failed"
    assert result.route_source == "light_rule"
    assert result.possible_rule_misclassification is True
    assert provider.call_count == 1


def test_stage1_light_rule_higher_capability_fallback_marks_misclassification():
    primary = MockProvider([MockScenario.UNAVAILABLE])
    higher_capability = MockProvider([_response()])
    router = Router(
        ModelRegistry(
            {
                "low_model": _model(
                    "low_model",
                    capability="low",
                    task_types=(TaskType.FILE_OPERATION,),
                    price=1,
                ),
                "high_model": _model(
                    "high_model",
                    capability="high",
                    task_types=(TaskType.FILE_OPERATION,),
                    price=5,
                ),
            }
        ),
        {"low_model": primary, "high_model": higher_capability},
        lambda request: (_ for _ in ()).throw(
            AssertionError("light rule must not invoke JEV")
        ),
        safe_default_model="low_model",
        estimated_max_costs={"low_model": 0.10, "high_model": 0.20},
    )

    result = router.route(
        RouteRequest(task_id="stage1-light-upgrade", prompt="read README.md")
    )

    assert result.status == "success"
    assert result.route_source == "fallback"
    assert result.selected_model == "high_model"
    assert result.possible_rule_misclassification is True
    assert primary.call_count == 1
    assert higher_capability.call_count == 1


def test_stage1_jev_route_calls_classifier_once_and_uses_policy_model():
    primary = MockProvider([_response()])
    classifier_calls = []
    classification = _classifier()

    def classify(request):
        classifier_calls.append(request)
        return classification

    router = Router(
        ModelRegistry(
            {
                "medium_model": _model("medium_model", capability="medium", price=1),
                "high_model": _model("high_model", capability="high", price=5),
            }
        ),
        {"medium_model": primary},
        classify,
        safe_default_model="high_model",
        estimated_max_costs={"medium_model": 0.10},
        classifier_cost_resolver=lambda request, value: _exact_cost(0.01),
    )

    result = router.route(
        RouteRequest(task_id="stage1-jev", prompt="debug the failing login flow")
    )

    assert result.status == "success"
    assert result.route_source == "jev"
    assert result.classifier == classification
    assert result.selected_model == "medium_model"
    assert result.possible_rule_misclassification is False
    assert len(classifier_calls) == 1
    assert primary.call_count == 1


def test_stage1_fallback_smoke_has_stable_history_and_cost():
    run = run_failure_injection(FailureInjectionCase.FALLBACK_SUCCESS)
    result = run.result

    assert result.status == "success"
    assert result.route_source == "fallback"
    assert result.fallback_history == [
        "primary:model_unavailable",
        "fallback_1:success",
    ]
    assert run.provider_call_counts == {"primary": 1, "fallback_1": 1}
    assert result.cost.fallback_cost > 0


def test_stage1_budget_guard_blocks_fallback_before_provider_call():
    run = run_failure_injection(FailureInjectionCase.BUDGET_BLOCK)
    result = run.result

    assert result.status == "failed"
    assert result.errors[-1].code == "budget_limit_reached"
    assert result.fallback_history == [
        "primary:provider_timeout",
        "fallback_1:budget_limit_reached",
    ]
    assert run.provider_call_counts == {
        "primary": 1,
        "fallback_1": 0,
        "fallback_2": 0,
    }


def test_stage1_fixture_repeatability_uses_fresh_initial_state(tmp_path):
    fixture = tmp_path / "temporary-t12-fixture"
    initial_state = fixture / "initial_state"
    initial_state.mkdir(parents=True)
    (fixture / "task.yaml").write_text(
        "id: stage1-fixture\n"
        "name: temporary repeatability smoke\n"
        "baseline_model: local-baseline\n"
        "acceptance:\n"
        "  mode: manual\n",
        encoding="utf-8",
    )
    (fixture / "prompt.md").write_text("Repeat the isolated task.\n", encoding="utf-8")
    (fixture / "acceptance.md").write_text("Inspect the isolated workspace.\n", encoding="utf-8")
    (fixture / "expected_constraints.md").write_text("No source mutation.\n", encoding="utf-8")
    (initial_state / "state.txt").write_text("original\n", encoding="utf-8")

    seen_initial_contents = []
    seen_workspaces = []

    def executor(workspace, metadata, prompt):
        seen_workspaces.append(workspace)
        seen_initial_contents.append((workspace / "state.txt").read_text(encoding="utf-8"))
        (workspace / "state.txt").write_text("mutated\n", encoding="utf-8")
        return {"status": "completed"}

    first = run_fixture(fixture, "baseline", baseline_executor=executor)
    second = run_fixture(fixture, "baseline", baseline_executor=executor)

    assert first["status"] == second["status"] == "pending_manual"
    assert first["workspace_initial_hash"] == second["workspace_initial_hash"]
    assert first["source_fixture_hash_before"] == second["source_fixture_hash_before"]
    assert first["source_fixture_hash_before"] == first["source_fixture_hash_after"]
    assert second["source_fixture_hash_before"] == second["source_fixture_hash_after"]
    assert first["source_unchanged"] is True
    assert second["source_unchanged"] is True
    assert first["workspace_removed"] is True
    assert second["workspace_removed"] is True
    assert seen_initial_contents == ["original\n", "original\n"]
    assert seen_workspaces[0] != seen_workspaces[1]
    assert (initial_state / "state.txt").read_text(encoding="utf-8") == "original\n"


def test_stage1_jsonl_logging_keeps_only_safe_fields_and_error_codes(tmp_path):
    secret = "sk-live-stage1-secret-123456"
    path = tmp_path / "runs.jsonl"
    result = RouteResult(
        task_id="stage1-logging",
        status="failed",
        route_source="jev",
        possible_rule_misclassification=True,
        classifier=_classifier(),
        selected_model="medium_model",
        fallback_history=["medium_model:provider_error"],
        acceptance={"prompt": "private input", "api_key": secret},
        errors=[RouteError(code="provider_error", message=f"secret={secret}")],
    )

    JsonlLoggingStore(path).append(result)
    record = json.loads(path.read_text(encoding="utf-8"))

    assert secret not in path.read_text(encoding="utf-8")
    assert record["possible_rule_misclassification"] is True
    assert record["error_codes"] == ["provider_error"]
    assert "acceptance" not in record
    assert "notes" not in record["classifier"]
    assert "message" not in record


def test_stage1_estimated_cost_uses_t07_public_contract():
    component = calculate_cost_from_usage(
        TokenUsage(input_tokens=None, output_tokens=50),
        input_price=2,
        output_price=3,
        estimated_usage=TokenUsage(input_tokens=100, output_tokens=50),
        estimation_source="heuristic",
    )
    breakdown = calculate_cost_breakdown(execution_cost=component)
    result = RouteResult(
        task_id="stage1-estimated-cost",
        status="success",
        route_source="manual_override",
        selected_model="mock_model",
        cost=breakdown,
    )

    assert result.cost.cost_estimated is True
    assert result.cost.cost_estimation_source == "heuristic"
    assert result.cost.total_production_cost == component.cost


def test_stage1_manual_override_budget_guard_does_not_call_provider():
    provider = MockProvider([MockScenario.SUCCESS])
    router = Router(
        ModelRegistry({"manual_model": _model("manual_model", capability="high")}),
        {"manual_model": provider},
        lambda request: _classifier(capability="high"),
        safe_default_model="manual_model",
        estimated_max_costs={"manual_model": 0.20},
    )

    result = router.route(
        RouteRequest(
            task_id="stage1-manual-budget",
            prompt="run the manually selected model",
            manual_model="manual_model",
            budget_limit=0.10,
        )
    )

    assert result.status == "failed"
    assert result.route_source == "manual_override"
    assert result.errors[0].code == "manual_model_budget_exceeded"
    assert result.possible_rule_misclassification is False
    assert provider.call_count == 0
