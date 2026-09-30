"""T-09 Router Core integration tests."""

from decimal import Decimal

import pytest

from jev_router.cost import CostComponent
from jev_router.errors import NoEligibleModelError, SchemaValidationError
from jev_router.providers import (
    MockProvider,
    MockScenario,
    ModelResponse,
    ProviderInvocationError,
    ProviderTimeoutError,
)
from jev_router.registry import ModelDefinition, ModelRegistry
from jev_router.router import Router
from jev_router.schemas import ClassifierResult, RouteRequest, TaskType


def response(text="answer", input_tokens=100, output_tokens=50):
    return ModelResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_request_id="request-id",
    )


def classifier(*, task_type=TaskType.CODING, capability="medium"):
    return ClassifierResult(
        task_type=task_type,
        difficulty_score=5,
        difficulty_bucket="medium",
        required_capability=capability,
        confidence=0.88,
        risk_level="medium",
    )


def model(
    name,
    *,
    capability="low",
    task_types=(TaskType.CODING,),
    price=1,
    enabled=True,
    model_id=None,
):
    return ModelDefinition(
        provider="test-provider",
        model_id=model_id or f"actual-{name}",
        capability=capability,
        task_types=list(task_types),
        input_cost_per_million=price,
        output_cost_per_million=price,
        enabled=enabled,
    )


def make_router(
    models,
    providers,
    *,
    jev=None,
    safe_default_model="safe_model",
    estimates=None,
    classifier_cost_resolver=None,
    classifier_failure_cost_resolver=None,
    rule_engine=None,
    policy_engine=None,
    sink=None,
):
    return Router(
        ModelRegistry(models),
        providers,
        jev or (lambda request: classifier()),
        safe_default_model=safe_default_model,
        estimated_max_costs=estimates,
        classifier_cost_resolver=classifier_cost_resolver,
        classifier_failure_cost_resolver=classifier_failure_cost_resolver,
        rule_engine=rule_engine,
        policy_engine=policy_engine,
        result_sink=sink,
    )


def request(prompt="implement the task", **overrides):
    payload = {"task_id": "task-09", "prompt": prompt}
    payload.update(overrides)
    return RouteRequest.model_validate(payload)


def exact_cost(value):
    return CostComponent(
        cost=value,
        cost_estimated=False,
        cost_estimation_source="provider_usage",
    )


def test_manual_success_skips_rule_jev_and_policy():
    class Exploding:
        def evaluate(self, request):
            raise AssertionError("rule must be skipped")

    jev_calls = []
    manual = MockProvider([response("manual")])
    router = make_router(
        {"manual_model": model("manual_model", capability="high")},
        {"manual_model": manual},
        jev=lambda value: jev_calls.append(value),
        rule_engine=Exploding(),
        safe_default_model="manual_model",
        estimates={"manual_model": 0.10},
    )

    result = router.route(request(manual_model="manual_model"))

    assert result.status == "success"
    assert result.route_source == "manual_override"
    assert result.selected_model == "manual_model"
    assert result.classifier is None
    assert jev_calls == []
    assert manual.call_count == 1


def test_manual_budget_block_does_not_call_or_substitute():
    manual = MockProvider([response("manual")])
    router = make_router(
        {"manual_model": model("manual_model")},
        {"manual_model": manual},
        safe_default_model="manual_model",
        estimates={"manual_model": Decimal("0.10")},
    )

    result = router.route(
        request(manual_model="manual_model", budget_limit=Decimal("0.09"))
    )

    assert result.status == "failed"
    assert result.errors[0].code == "manual_model_budget_exceeded"
    assert result.selected_model is None
    assert manual.call_count == 0


def test_light_rule_policy_primary_success_hides_synthetic_classifier():
    primary = MockProvider([response(input_tokens=10, output_tokens=5)])
    router = make_router(
        {
            "low_model": model(
                "low_model", capability="low", task_types=(TaskType.FILE_OPERATION,), price=2
            )
        },
        {"low_model": primary},
        safe_default_model="low_model",
        estimates={"low_model": 0.10},
    )

    result = router.route(request("read README.md"))

    assert result.status == "success"
    assert result.route_source == "light_rule"
    assert result.rule_id == "simple_file_read"
    assert result.classifier is None
    assert result.selected_model == "low_model"


def test_rule_miss_jev_policy_primary_success_exposes_real_classifier():
    primary = MockProvider([response(input_tokens=10, output_tokens=5)])
    seen = []
    router = make_router(
        {"medium_model": model("medium_model", capability="medium", price=2)},
        {"medium_model": primary},
        jev=lambda value: classifier(),
        estimates={"medium_model": 0.10},
        classifier_cost_resolver=lambda req, value: seen.append(value) or exact_cost(0.01),
    )

    result = router.route(request("debug the failing login flow"))

    assert result.route_source == "jev"
    assert result.classifier == classifier()
    assert result.cost.classifier_cost == 0.01
    assert seen == [classifier()]


@pytest.mark.parametrize(
    "failure",
    [
        SchemaValidationError("JEV classifier response is not valid JSON"),
        SchemaValidationError("JEV classifier response failed schema validation"),
        ProviderTimeoutError(),
        ProviderInvocationError(),
    ],
)
def test_jev_failures_use_explicit_safe_default_not_cheapest(failure):
    safe = MockProvider([response("safe")])
    cheap = MockProvider([response("cheap")])

    def fail(_request):
        raise failure

    router = make_router(
        {
            "cheap_model": model("cheap_model", capability="low", price=0),
            "safe_model": model("safe_model", capability="high", price=10),
        },
        {"cheap_model": cheap, "safe_model": safe},
        jev=fail,
        safe_default_model="safe_model",
        estimates={"cheap_model": 0.01, "safe_model": 0.20},
        classifier_failure_cost_resolver=lambda req, code: exact_cost(0.01),
    )

    result = router.route(request("perform an ambiguous task"))

    assert result.status == "success"
    assert result.route_source == "safe_default"
    assert result.selected_model == "safe_model"
    assert cheap.call_count == 0
    assert safe.call_count == 1
    assert result.errors[0].code in {
        "jev_invalid_response",
        "jev_schema_error",
        "jev_timeout",
        "jev_provider_error",
    }


def test_jev_invalid_payload_is_safe_default():
    safe = MockProvider([response("safe")])
    router = make_router(
        {"safe_model": model("safe_model", capability="high")},
        {"safe_model": safe},
        jev=lambda request: {"not": "a classifier"},
        safe_default_model="safe_model",
        estimates={"safe_model": 0.10},
        classifier_failure_cost_resolver=lambda req, code: exact_cost(0.01),
    )

    result = router.route(request("do something unclear"))

    assert result.status == "success"
    assert result.errors[0].code == "jev_schema_error"


def test_primary_failure_then_fallback_success_has_stable_history_and_real_cost():
    primary = MockProvider([MockScenario.UNAVAILABLE])
    fallback = MockProvider([response("fallback", input_tokens=1000, output_tokens=500)])
    router = make_router(
        {
            "primary_model": model("primary_model", capability="medium", price=2),
            "fallback_model": model("fallback_model", capability="high", price=4),
        },
        {"primary_model": primary, "fallback_model": fallback},
        jev=lambda request: classifier(capability="medium"),
        estimates={"primary_model": 0.10, "fallback_model": 0.20},
        classifier_cost_resolver=lambda req, value: exact_cost(0.01),
    )

    result = router.route(request("fix the failing code"))

    assert result.status == "success"
    assert result.route_source == "fallback"
    assert result.selected_model == "fallback_model"
    assert result.classifier == classifier(capability="medium")
    assert result.fallback_history == [
        "primary_model:model_unavailable",
        "fallback_model:success",
    ]
    assert result.cost.classifier_cost == 0.01
    assert result.cost.execution_cost == 0
    assert result.cost.fallback_cost == 0.006
    assert result.cost.total_production_cost == 0.016
    assert result.cost.cost_estimated is False
    assert primary.call_count == 1
    assert fallback.call_count == 1


def test_primary_failure_blocks_fallback_before_provider_call():
    primary = MockProvider([MockScenario.TIMEOUT])
    fallback = MockProvider([response("fallback")])
    router = make_router(
        {
            "primary_model": model("primary_model", capability="medium", price=1),
            "fallback_model": model("fallback_model", capability="high", price=2),
        },
        {"primary_model": primary, "fallback_model": fallback},
        jev=lambda request: classifier(capability="medium"),
        estimates={"primary_model": 0.01, "fallback_model": 0.10},
        classifier_cost_resolver=lambda req, value: exact_cost(0.04),
    )

    result = router.route(request("fix the failing code", budget_limit=0.05))

    assert result.status == "failed"
    assert result.errors[-1].code == "budget_limit_reached"
    assert result.fallback_history == [
        "primary_model:provider_timeout",
        "fallback_model:budget_limit_reached",
    ]
    assert primary.call_count == 1
    assert fallback.call_count == 0


def test_no_eligible_model_returns_stable_failure_without_provider_call():
    provider = MockProvider([response()])

    class NoModels:
        def decide(self, value):
            raise NoEligibleModelError("do not expose this detail")

    router = make_router(
        {"model": model("model", capability="high")},
        {"model": provider},
        policy_engine=NoModels(),
        safe_default_model="model",
        classifier_cost_resolver=lambda req, value: exact_cost(0.0),
    )

    result = router.route(request("do something unclear"))

    assert result.status == "failed"
    assert result.errors[0].code == "no_eligible_model"
    assert result.errors[0].message == "No eligible model"
    assert provider.call_count == 0


def test_missing_provider_is_stable_model_unavailable():
    router = make_router(
        {"model": model("model", capability="medium")},
        {},
        jev=lambda request: classifier(capability="medium"),
        estimates={"model": 0.1},
        classifier_cost_resolver=lambda req, value: exact_cost(0.0),
    )

    result = router.route(request("fix the failing code"))

    assert result.status == "failed"
    assert result.errors[0].code == "model_unavailable"
    assert result.fallback_history == ["model:model_unavailable"]


def test_sink_receives_each_terminal_result_once():
    seen = []
    provider = MockProvider([response()])
    router = make_router(
        {"model": model("model", task_types=(TaskType.FILE_OPERATION,))},
        {"model": provider},
        safe_default_model="model",
        sink=seen.append,
        estimates={"model": 0.1},
    )

    result = router.route(request("read README.md", budget_limit=None))

    assert seen == [result]
    assert len(seen) == 1


def test_sink_failure_is_not_swallowed_or_retried():
    provider = MockProvider([response()])
    sink_calls = []

    def sink(result):
        sink_calls.append(result)
        raise RuntimeError("sink is external")

    router = make_router(
        {"model": model("model", task_types=(TaskType.FILE_OPERATION,))},
        {"model": provider},
        safe_default_model="model",
        sink=sink,
        estimates={"model": 0.1},
    )

    with pytest.raises(RuntimeError, match="sink is external"):
        router.route(request("read README.md"))

    assert provider.call_count == 1
    assert len(sink_calls) == 1


def test_classifier_cost_is_only_resolved_on_jev_path():
    calls = []
    light_provider = MockProvider([response()])
    router = make_router(
        {"model": model("model", task_types=(TaskType.FILE_OPERATION,))},
        {"model": light_provider},
        safe_default_model="model",
        estimates={"model": 0.1},
        classifier_cost_resolver=lambda req, value: calls.append(1) or exact_cost(0.5),
    )

    result = router.route(request("read README.md"))

    assert result.cost.classifier_cost == 0
    assert calls == []


def test_estimated_budget_value_is_not_written_to_production_cost():
    provider = MockProvider([response(input_tokens=1, output_tokens=1)])
    router = make_router(
        {"model": model("model", task_types=(TaskType.FILE_OPERATION,), price=2)},
        {"model": provider},
        safe_default_model="model",
        estimates={"model": 99.0},
    )

    result = router.route(request("read README.md", budget_limit=None))

    assert result.status == "success"
    assert result.cost.execution_cost == 0.000004
    assert result.cost.total_production_cost == 0.000004
    assert result.cost.cost_estimated is False


def test_safe_default_failure_is_terminal_and_stable():
    safe = MockProvider([MockScenario.TIMEOUT])
    router = make_router(
        {"safe_model": model("safe_model", capability="high")},
        {"safe_model": safe},
        jev=lambda request: (_ for _ in ()).throw(ProviderTimeoutError()),
        safe_default_model="safe_model",
        estimates={"safe_model": 0.1},
        classifier_failure_cost_resolver=lambda req, code: exact_cost(0.01),
    )

    result = router.route(request("unclear task"))

    assert result.status == "failed"
    assert result.route_source == "safe_default"
    assert result.errors[0].code == "jev_timeout"
    assert result.errors[1].code == "safe_default_failed"
    assert result.errors[2].code == "provider_timeout"
    assert result.fallback_history == ["safe_model:provider_timeout"]
    assert safe.call_count == 1


def test_budget_estimate_missing_with_limit_stops_before_provider():
    provider = MockProvider([response()])
    router = make_router(
        {"model": model("model", task_types=(TaskType.FILE_OPERATION,))},
        {"model": provider},
        safe_default_model="model",
    )

    result = router.route(request("read README.md", budget_limit=0.10))

    assert result.status == "failed"
    assert result.errors[0].code == "budget_estimate_unavailable"
    assert provider.call_count == 0


def test_budget_estimate_missing_mapping_key_stops_before_provider():
    provider = MockProvider([response()])
    router = make_router(
        {"model": model("model", task_types=(TaskType.FILE_OPERATION,))},
        {"model": provider},
        safe_default_model="model",
        estimates={"other_model": 0.10},
    )

    result = router.route(request("read README.md", budget_limit=0.10))

    assert result.errors[0].code == "budget_estimate_unavailable"
    assert provider.call_count == 0


def test_budget_limit_none_does_not_require_an_estimate():
    provider = MockProvider([response()])
    router = make_router(
        {"model": model("model", task_types=(TaskType.FILE_OPERATION,))},
        {"model": provider},
        safe_default_model="model",
    )

    result = router.route(request("read README.md", budget_limit=None))

    assert result.status == "success"
    assert provider.call_count == 1


def test_budget_estimate_equality_is_allowed_for_manual_model():
    provider = MockProvider([response("manual")])
    router = make_router(
        {"model": model("model")},
        {"model": provider},
        safe_default_model="model",
        estimates={"model": 0.10},
    )

    result = router.route(
        request(manual_model="model", budget_limit=0.10)
    )

    assert result.status == "success"
    assert provider.call_count == 1


def test_failed_primary_exposure_blocks_fallback_without_inventing_cost():
    primary = MockProvider([MockScenario.TIMEOUT])
    fallback = MockProvider([response("fallback")])
    router = make_router(
        {
            "primary_model": model("primary_model", capability="medium", price=1),
            "fallback_model": model("fallback_model", capability="high", price=2),
        },
        {"primary_model": primary, "fallback_model": fallback},
        jev=lambda req: classifier(capability="medium"),
        estimates={"primary_model": 0.10, "fallback_model": 0.10},
        classifier_cost_resolver=lambda req, value: exact_cost(0.0),
    )

    result = router.route(request("fix the failing code", budget_limit=0.15))

    assert result.status == "failed"
    assert result.errors[-1].code == "budget_limit_reached"
    assert result.fallback_history == [
        "primary_model:provider_timeout",
        "fallback_model:budget_limit_reached",
    ]
    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert result.cost.execution_cost == 0
    assert result.cost.fallback_cost == 0
    assert result.cost.total_production_cost == 0


def test_failed_primary_exposure_allows_fallback_at_equality():
    primary = MockProvider([MockScenario.TIMEOUT])
    fallback = MockProvider([response("fallback", input_tokens=1000, output_tokens=500)])
    router = make_router(
        {
            "primary_model": model("primary_model", capability="medium", price=1),
            "fallback_model": model("fallback_model", capability="high", price=4),
        },
        {"primary_model": primary, "fallback_model": fallback},
        jev=lambda req: classifier(capability="medium"),
        estimates={"primary_model": 0.10, "fallback_model": 0.10},
        classifier_cost_resolver=lambda req, value: exact_cost(0.0),
    )

    result = router.route(request("fix the failing code", budget_limit=0.20))

    assert result.status == "success"
    assert result.route_source == "fallback"
    assert result.cost.execution_cost == 0
    assert result.cost.fallback_cost == 0.006
    assert result.cost.total_production_cost == 0.006
    assert fallback.call_count == 1


def test_original_pilot_budget_blocks_high_after_medium_timeout_with_classifier_cost():
    primary = MockProvider([MockScenario.TIMEOUT])
    fallback = MockProvider([response("fallback")])
    router = make_router(
        {
            "medium_model": model("medium_model", capability="medium", price=1),
            "high_model": model("high_model", capability="high", price=2),
        },
        {"medium_model": primary, "high_model": fallback},
        jev=lambda request: classifier(capability="medium"),
        estimates={"medium_model": 0.25, "high_model": 1.00},
        classifier_cost_resolver=lambda req, value: exact_cost(0.00004),
    )

    result = router.route(request("fix the failing code", budget_limit=1.25))

    assert result.status == "failed"
    assert result.errors[-1].code == "budget_limit_reached"
    assert result.fallback_history == [
        "medium_model:provider_timeout",
        "high_model:budget_limit_reached",
    ]
    assert primary.call_count == 1
    assert fallback.call_count == 0


def test_adjust_budget_allows_high_after_medium_timeout_with_conservative_exposure():
    primary = MockProvider([MockScenario.TIMEOUT])
    fallback = MockProvider([response("fallback")])
    router = make_router(
        {
            "medium_model": model("medium_model", capability="medium", price=1),
            "high_model": model("high_model", capability="high", price=2),
        },
        {"medium_model": primary, "high_model": fallback},
        jev=lambda request: classifier(capability="medium"),
        estimates={"medium_model": 0.25, "high_model": 1.00},
        classifier_cost_resolver=lambda req, value: exact_cost(0.00004),
    )

    result = router.route(request("fix the failing code", budget_limit=1.50))

    assert result.status == "success"
    assert result.route_source == "fallback"
    assert result.selected_model == "high_model"
    assert result.fallback_history == [
        "medium_model:provider_timeout",
        "high_model:success",
    ]
    assert primary.call_count == 1
    assert fallback.call_count == 1


def test_jev_success_without_cost_resolver_stops_before_execution():
    provider = MockProvider([response()])
    router = make_router(
        {"model": model("model", capability="medium")},
        {"model": provider},
        jev=lambda req: classifier(capability="medium"),
        estimates={"model": 0.10},
    )

    result = router.route(request("fix the failing code"))

    assert result.status == "failed"
    assert result.route_source == "jev"
    assert result.errors[0].code == "classifier_cost_unavailable"
    assert result.cost.cost_estimated is True
    assert provider.call_count == 0


def test_jev_failure_without_failure_cost_resolver_does_not_call_safe_default():
    safe = MockProvider([response("safe")])
    router = make_router(
        {"safe_model": model("safe_model", capability="high")},
        {"safe_model": safe},
        jev=lambda req: (_ for _ in ()).throw(ProviderTimeoutError()),
        safe_default_model="safe_model",
        estimates={"safe_model": 0.10},
    )

    result = router.route(request("unclear task"))

    assert result.status == "failed"
    assert result.route_source == "safe_default"
    assert [error.code for error in result.errors] == [
        "jev_timeout",
        "classifier_cost_unavailable",
    ]
    assert safe.call_count == 0


@pytest.mark.parametrize(
    "cost_component,expected_estimated",
    [
        (exact_cost(0.01), False),
        (
            CostComponent(
                cost=0.01,
                cost_estimated=True,
                cost_estimation_source="heuristic",
            ),
            True,
        ),
    ],
)
def test_jev_failure_cost_evidence_is_included_with_precision(
    cost_component, expected_estimated
):
    safe = MockProvider([response("safe", input_tokens=1000, output_tokens=500)])
    router = make_router(
        {"safe_model": model("safe_model", capability="high", price=4)},
        {"safe_model": safe},
        jev=lambda req: (_ for _ in ()).throw(ProviderTimeoutError()),
        safe_default_model="safe_model",
        estimates={"safe_model": 0.10},
        classifier_failure_cost_resolver=lambda req, code: cost_component,
    )

    result = router.route(request("unclear task"))

    assert result.status == "success"
    assert result.cost.classifier_cost == 0.01
    assert result.cost.execution_cost == 0.006
    assert result.cost.total_production_cost == 0.016
    assert result.cost.cost_estimated is expected_estimated
    assert safe.call_count == 1


def test_provider_receives_configured_model_id_not_logical_alias():
    provider = MockProvider([response()])
    router = make_router(
        {
            "friendly_name": model(
                "friendly_name",
                task_types=(TaskType.FILE_OPERATION,),
                model_id="actual-provider-id",
            )
        },
        {"friendly_name": provider},
        safe_default_model="friendly_name",
        estimates={"friendly_name": 0.10},
    )

    result = router.route(request("read README.md"))

    assert result.status == "success"
    assert provider.requests[0].model_id == "actual-provider-id"
