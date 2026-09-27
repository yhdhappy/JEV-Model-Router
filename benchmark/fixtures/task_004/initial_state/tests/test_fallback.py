"""T-08 fallback, safe-default, and budget-guard contract tests."""

import pytest

from jev_router.cost import CostComponent
from jev_router.errors import SchemaValidationError
from jev_router.fallback import (
    BudgetDecision,
    FallbackResult,
    JEVFallbackResult,
    check_budget,
    execute_jev_with_fallback,
    execute_manual_model,
    execute_model_fallback,
    normalize_execution_failure,
    normalize_jev_failure,
    safe_jev_fallback,
)
from jev_router.providers import (
    MockProvider,
    MockScenario,
    ModelRequest,
    ModelResponse,
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


def make_request(**overrides):
    payload = {
        "model_id": "primary_model",
        "prompt": "run the task",
    }
    payload.update(overrides)
    return ModelRequest.model_validate(payload)


def success_response(text="fallback answer"):
    return ModelResponse(
        text=text,
        input_tokens=10,
        output_tokens=20,
        provider_request_id="mock-request",
    )


def exact_cost(amount):
    return CostComponent(
        cost=amount,
        cost_estimated=False,
        cost_estimation_source="provider_usage",
    )


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("timeout", "jev_timeout"),
        ("network_error", "jev_network_error"),
        ("invalid_json", "jev_invalid_response"),
        ("schema_error", "jev_schema_error"),
        ("provider_error", "jev_provider_error"),
        (ProviderTimeoutError(), "jev_timeout"),
        (ConnectionError("network details"), "jev_network_error"),
        (
            SchemaValidationError("JEV classifier response is not valid JSON"),
            "jev_invalid_response",
        ),
        (
            SchemaValidationError(
                "JEV classifier response failed schema validation"
            ),
            "jev_schema_error",
        ),
    ],
)
def test_jev_failure_reasons_are_normalized_without_echoing_failure_details(
    failure, expected
):
    reason = normalize_jev_failure(failure)

    assert reason == expected
    assert "network details" not in reason


def test_jev_safe_fallback_only_emits_a_signal_and_does_not_select_a_model():
    result = safe_jev_fallback("invalid_json")

    assert isinstance(result, JEVFallbackResult)
    assert result.status == "safe_default"
    assert result.decision == "safe_default_fallback"
    assert result.fallback_reason == "jev_invalid_response"
    assert result.error_code == "jev_invalid_response"
    assert result.selected_model is None


def test_jev_wrapper_returns_safe_default_on_classifier_failure():
    result = execute_jev_with_fallback(
        lambda: (_ for _ in ()).throw(
            SchemaValidationError("JEV classifier response failed schema validation")
        )
    )

    assert result == safe_jev_fallback("schema_error")


def test_jev_wrapper_preserves_success_and_does_not_route():
    classification = {"task_type": "coding"}

    result = execute_jev_with_fallback(lambda: classification)

    assert result.status == "success"
    assert result.value == classification
    assert result.fallback_reason is None
    assert result.selected_model is None


@pytest.mark.parametrize(
    "current,estimated,limit,allowed",
    [
        (0.10, 0.20, 0.30, True),
        (0.10, 0.21, 0.30, False),
        (0.10, 0.20, None, True),
    ],
)
def test_budget_guard_allows_unset_limit_and_exact_boundary(
    current, estimated, limit, allowed
):
    decision = check_budget(current, estimated, limit)

    assert isinstance(decision, BudgetDecision)
    assert decision.allowed is allowed
    assert decision.error_code is None if allowed else decision.error_code == (
        "budget_limit_reached"
    )


def test_budget_guard_rejects_negative_or_non_finite_values():
    with pytest.raises(ValueError):
        check_budget(-0.01, 0.01, 1.0)
    with pytest.raises(ValueError):
        check_budget(0.0, float("inf"), 1.0)


def test_primary_failure_then_fallback_success_records_failure_and_cost():
    primary = MockProvider([MockScenario.UNAVAILABLE])
    fallback = MockProvider([success_response()])

    result = execute_model_fallback(
        candidates=["primary_model", "fallback_1"],
        providers={"primary_model": primary, "fallback_1": fallback},
        request=make_request(),
        current_accumulated_cost=0.0,
        estimated_next_max_cost={"primary_model": 0.10, "fallback_1": 0.10},
        budget_limit=0.20,
        cost_by_model={
            "primary_model": exact_cost(0.01),
            "fallback_1": exact_cost(0.02),
        },
    )

    assert isinstance(result, FallbackResult)
    assert result.status == "success"
    assert result.selected_model == "fallback_1"
    assert result.response.text == "fallback answer"
    assert result.fallback_reason == "model_unavailable"
    assert result.error_code is None
    assert [attempt.model for attempt in result.fallback_history] == [
        "primary_model",
        "fallback_1",
    ]
    assert result.fallback_history[0].error_code == "model_unavailable"
    assert result.fallback_history[0].called is True
    assert result.fallback_history[1].budget_decision.allowed is True
    assert result.fallback_history[1].called is True
    assert result.fallback_cost.cost == 0.02
    assert result.fallback_cost.cost_estimated is False
    assert primary.call_count == 1
    assert fallback.call_count == 1


def test_budget_blocks_next_model_before_provider_call():
    primary = MockProvider([MockScenario.TIMEOUT])
    fallback = MockProvider([success_response()])

    result = execute_model_fallback(
        candidates=["primary_model", "fallback_1"],
        providers={"primary_model": primary, "fallback_1": fallback},
        request=make_request(),
        current_accumulated_cost=0.0,
        estimated_next_max_cost={"primary_model": 0.01, "fallback_1": 0.10},
        budget_limit=0.05,
        cost_by_model={"primary_model": exact_cost(0.01)},
    )

    assert result.status == "stopped"
    assert result.error_code == "budget_limit_reached"
    assert result.fallback_reason == "budget_limit_reached"
    assert result.selected_model is None
    assert result.fallback_history[0].error_code == "provider_timeout"
    assert result.fallback_history[1].error_code == "budget_limit_reached"
    assert result.fallback_history[1].called is False
    assert result.fallback_history[1].budget_decision.allowed is False
    assert primary.call_count == 1
    assert fallback.call_count == 0


def test_budget_guard_is_applied_to_each_candidate_and_none_allows_fallback():
    primary = MockProvider([MockScenario.ERROR])
    fallback = MockProvider([success_response()])

    result = execute_model_fallback(
        candidates=["primary_model", "fallback_1"],
        providers={"primary_model": primary, "fallback_1": fallback},
        request=make_request(),
        current_accumulated_cost=0.05,
        estimated_next_max_cost=0.10,
        budget_limit=None,
    )

    assert result.status == "success"
    assert result.fallback_history[1].budget_decision.budget_limit is None
    assert fallback.call_count == 1


def test_duplicate_candidates_are_never_called_twice():
    primary = MockProvider([MockScenario.ERROR])
    fallback = MockProvider([MockScenario.ERROR])

    result = execute_model_fallback(
        candidates=["primary_model", "fallback_1", "fallback_1"],
        providers={"primary_model": primary, "fallback_1": fallback},
        request=make_request(),
        current_accumulated_cost=0.0,
        estimated_next_max_cost=0.10,
        budget_limit=None,
    )

    assert result.status == "failed"
    assert [attempt.model for attempt in result.fallback_history] == [
        "primary_model",
        "fallback_1",
    ]
    assert fallback.call_count == 1


@pytest.mark.parametrize(
    "failure,expected",
    [
        (ProviderUnavailableError(), "model_unavailable"),
        (ProviderTimeoutError(), "provider_timeout"),
        (ProviderInvocationError(), "provider_error"),
        (RuntimeError("unexpected provider detail"), "provider_error"),
    ],
)
def test_execution_failures_are_normalized_to_provider_contract(failure, expected):
    assert normalize_execution_failure(failure) == expected


def test_all_execution_failures_return_failed_without_unbounded_retry():
    providers = {
        "primary_model": MockProvider([MockScenario.ERROR]),
        "fallback_1": MockProvider([MockScenario.TIMEOUT]),
    }

    result = execute_model_fallback(
        candidates=["primary_model", "fallback_1"],
        providers=providers,
        request=make_request(),
        current_accumulated_cost=0.0,
        estimated_next_max_cost=0.10,
        budget_limit=None,
    )

    assert result.status == "failed"
    assert result.error_code == "provider_timeout"
    assert result.fallback_reason == "provider_error"
    assert sum(provider.call_count for provider in providers.values()) == 2


def test_manual_model_budget_exceeded_does_not_call_or_replace_model():
    manual = MockProvider([success_response("manual answer")])

    result = execute_manual_model(
        model="manual_model",
        provider=manual,
        request=make_request(model_id="manual_model"),
        current_accumulated_cost=0.20,
        estimated_next_max_cost=0.10,
        budget_limit=0.25,
    )

    assert result.status == "stopped"
    assert result.error_code == "manual_model_budget_exceeded"
    assert result.selected_model is None
    assert result.replacement_model is None
    assert manual.call_count == 0


def test_manual_model_is_called_when_budget_allows_and_never_falls_back():
    manual = MockProvider([success_response("manual answer")])

    result = execute_manual_model(
        model="manual_model",
        provider=manual,
        request=make_request(model_id="manual_model"),
        current_accumulated_cost=0.10,
        estimated_next_max_cost=0.10,
        budget_limit=0.20,
        cost=exact_cost(0.10),
    )

    assert result.status == "success"
    assert result.selected_model == "manual_model"
    assert result.replacement_model is None
    assert result.response.text == "manual answer"
    assert result.cost.cost == 0.10
    assert manual.call_count == 1


def test_manual_provider_failure_is_returned_without_substitute_model():
    manual = MockProvider([MockScenario.UNAVAILABLE])

    result = execute_manual_model(
        model="manual_model",
        provider=manual,
        request=make_request(model_id="manual_model"),
        current_accumulated_cost=0.0,
        estimated_next_max_cost=0.10,
        budget_limit=None,
    )

    assert result.status == "failed"
    assert result.error_code == "model_unavailable"
    assert result.replacement_model is None
    assert manual.call_count == 1
