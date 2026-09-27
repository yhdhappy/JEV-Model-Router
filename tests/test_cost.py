"""T-07 production token-cost contract tests."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from jev_router.cost import (
    CostComponent,
    TokenUsage,
    calculate_cost_breakdown,
    calculate_cost_from_response,
    calculate_cost_from_usage,
    calculate_model_cost,
)
from jev_router.providers import ModelResponse
from jev_router.schemas import CostBreakdown


def test_model_cost_uses_input_and_output_prices_per_million_tokens():
    assert calculate_model_cost(1_500_000, 250_000, 2.5, 4) == 4.75


def test_model_cost_accepts_zero_tokens_and_zero_prices():
    assert calculate_model_cost(0, 0, 0, 0) == 0.0
    assert calculate_model_cost(100, 200, 0, 0) == 0.0


@pytest.mark.parametrize(
    "input_tokens,output_tokens,input_price,output_price",
    [
        (-1, 0, 1, 1),
        (0, -1, 1, 1),
        (0, 0, -1, 1),
        (0, 0, 1, -1),
    ],
)
def test_model_cost_rejects_negative_tokens_or_prices(
    input_tokens, output_tokens, input_price, output_price
):
    with pytest.raises(ValueError, match="non-negative"):
        calculate_model_cost(
            input_tokens, output_tokens, input_price, output_price
        )


def test_decimal_prices_use_decimal_intermediate_without_rounding_policy():
    result = calculate_model_cost(
        1,
        1,
        Decimal("0.1"),
        Decimal("0.2"),
    )

    assert result == 0.0000003


def test_complete_provider_response_is_exact_and_records_provider_usage():
    component = calculate_cost_from_response(
        ModelResponse(text="answer", input_tokens=1_000, output_tokens=500),
        input_price=2,
        output_price=3,
    )

    assert component == CostComponent(
        cost=0.0035,
        cost_estimated=False,
        cost_estimation_source="provider_usage",
    )


def test_provider_reported_cost_wins_over_static_registry_prices():
    component = calculate_cost_from_response(
        ModelResponse(
            text="answer",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            provider_reported_cost=0.17,
        ),
        input_price=0.15,
        output_price=0.50,
    )

    assert component == CostComponent(
        cost=0.17,
        cost_estimated=False,
        cost_estimation_source="provider_usage",
    )


def test_variable_provider_without_reported_cost_is_an_estimate():
    component = calculate_cost_from_response(
        ModelResponse(
            text="answer",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            provider_pricing_is_variable=True,
        ),
        input_price=0.15,
        output_price=0.50,
    )

    assert component == CostComponent(
        cost=0.65,
        cost_estimated=True,
        cost_estimation_source="heuristic",
    )


def test_complete_usage_can_be_marked_as_local_tokenizer_estimate():
    component = calculate_cost_from_usage(
        TokenUsage(input_tokens=1_000, output_tokens=500),
        input_price=2,
        output_price=3,
        estimation_source="local_tokenizer",
    )

    assert component.cost == 0.0035
    assert component.cost_estimated is True
    assert component.cost_estimation_source == "local_tokenizer"


def test_incomplete_provider_usage_requires_explicit_estimated_usage_and_source():
    component = calculate_cost_from_usage(
        TokenUsage(input_tokens=None, output_tokens=500),
        input_price=2,
        output_price=3,
        estimated_usage=TokenUsage(input_tokens=1_000, output_tokens=500),
        estimation_source="heuristic",
    )

    assert component.cost == 0.0035
    assert component.cost_estimated is True
    assert component.cost_estimation_source == "heuristic"

    with pytest.raises(ValueError, match="estimation_source"):
        calculate_cost_from_usage(
            TokenUsage(input_tokens=None, output_tokens=500),
            input_price=2,
            output_price=3,
            estimated_usage=TokenUsage(input_tokens=1_000, output_tokens=500),
        )


def test_incomplete_provider_usage_cannot_be_silently_treated_as_exact():
    with pytest.raises(ValueError, match="complete estimated_usage"):
        calculate_cost_from_usage(
            TokenUsage(input_tokens=None, output_tokens=500),
            input_price=2,
            output_price=3,
        )


def test_usage_and_estimated_usage_reject_negative_values():
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-1, output_tokens=0)
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=0, output_tokens=-1)


def test_cost_breakdown_sums_classifier_execution_and_fallback():
    breakdown = calculate_cost_breakdown(
        classifier_cost=CostComponent(
            cost=0.001,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        ),
        execution_cost=CostComponent(
            cost=0.02,
            cost_estimated=True,
            cost_estimation_source="local_tokenizer",
        ),
        fallback_cost=CostComponent(cost=0.003),
    )

    assert breakdown == CostBreakdown(
        classifier_cost=0.001,
        execution_cost=0.02,
        fallback_cost=0.003,
        total_production_cost=0.024,
        cost_estimated=True,
        cost_estimation_source="local_tokenizer",
    )


def test_cost_breakdown_is_exact_when_all_components_are_exact():
    breakdown = calculate_cost_breakdown(
        classifier_cost=CostComponent(
            cost=0.001,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        ),
        execution_cost=CostComponent(
            cost=0.02,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        ),
    )

    assert breakdown.cost_estimated is False
    assert breakdown.cost_estimation_source == "provider_usage"
    assert breakdown.total_production_cost == 0.021


def test_mixed_estimation_sources_are_rejected_instead_of_collapsed():
    with pytest.raises(ValueError, match="multiple cost estimation sources"):
        calculate_cost_breakdown(
            classifier_cost=CostComponent(
                cost=0.001,
                cost_estimated=True,
                cost_estimation_source="heuristic",
            ),
            execution_cost=CostComponent(
                cost=0.02,
                cost_estimated=True,
                cost_estimation_source="local_tokenizer",
            ),
        )


def test_cost_breakdown_rejects_a_mismatched_total():
    with pytest.raises(ValidationError, match="total_production_cost"):
        CostBreakdown(
            classifier_cost=0.001,
            execution_cost=0.02,
            fallback_cost=0.003,
            total_production_cost=0.99,
        )


def test_cost_breakdown_accepts_decimal_sum_for_large_valid_amounts():
    breakdown = calculate_cost_breakdown(
        classifier_cost=CostComponent(cost=10_000.1),
        execution_cost=CostComponent(cost=13_000.130000000001),
        fallback_cost=CostComponent(cost=9_000.09),
    )

    assert breakdown.total_production_cost == 32_000.32


def test_cost_component_rejects_inconsistent_estimation_metadata():
    with pytest.raises(ValidationError):
        CostComponent(
            cost=0.01,
            cost_estimated=False,
            cost_estimation_source="local_tokenizer",
        )
    with pytest.raises(ValidationError):
        CostComponent(
            cost=0.01,
            cost_estimated=True,
            cost_estimation_source="provider_usage",
        )
