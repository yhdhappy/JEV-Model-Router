"""Deterministic production token-cost calculations for T-07."""

from decimal import Decimal, InvalidOperation
from typing import Literal, Optional, Union

from pydantic import Field, StrictInt, model_validator

from .providers import ModelResponse
from .schemas import CostBreakdown, StrictModel


TOKENS_PER_MILLION = Decimal("1000000")
CostEstimationSource = Literal[
    "provider_usage", "local_tokenizer", "heuristic"
]
Price = Union[int, float, Decimal]


class TokenUsage(StrictModel):
    """Optional raw usage input used when a Provider response is incomplete."""

    input_tokens: Optional[StrictInt] = Field(default=None, ge=0)
    output_tokens: Optional[StrictInt] = Field(default=None, ge=0)

    @property
    def is_complete(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None


class CostComponent(StrictModel):
    """One production-cost component with explicit precision provenance."""

    cost: float = Field(ge=0)
    cost_estimated: bool = False
    cost_estimation_source: Optional[CostEstimationSource] = None

    @model_validator(mode="after")
    def validate_estimation_metadata(self) -> "CostComponent":
        if self.cost_estimated:
            if self.cost_estimation_source not in {
                "local_tokenizer",
                "heuristic",
            }:
                raise ValueError(
                    "estimated cost requires local_tokenizer or heuristic "
                    "cost_estimation_source"
                )
        elif self.cost_estimation_source not in {None, "provider_usage"}:
            raise ValueError(
                "exact cost may only use provider_usage or no "
                "cost_estimation_source"
            )
        return self


def calculate_model_cost(
    input_tokens: int,
    output_tokens: int,
    input_price: Price,
    output_price: Price,
) -> float:
    """Calculate one model call cost using prices per million tokens.

    Decimal arithmetic is used for the intermediate calculation.  The public
    result remains a float to match ``CostBreakdown``; no decimal quantization
    or silent rounding is applied.
    """

    _validate_token_count(input_tokens, "input_tokens")
    _validate_token_count(output_tokens, "output_tokens")
    input_price_decimal = _validate_price(input_price, "input_price")
    output_price_decimal = _validate_price(output_price, "output_price")

    cost = (
        Decimal(input_tokens) / TOKENS_PER_MILLION * input_price_decimal
        + Decimal(output_tokens) / TOKENS_PER_MILLION * output_price_decimal
    )
    return float(cost)


def calculate_cost_from_response(
    response: ModelResponse,
    input_price: Price,
    output_price: Price,
) -> CostComponent:
    """Calculate an exact component from a normalized Provider response."""

    if not isinstance(response, ModelResponse):
        raise TypeError("response must be a ModelResponse")
    return CostComponent(
        cost=calculate_model_cost(
            response.input_tokens,
            response.output_tokens,
            input_price,
            output_price,
        ),
        cost_estimated=False,
        cost_estimation_source="provider_usage",
    )


def calculate_cost_from_usage(
    usage: TokenUsage,
    input_price: Price,
    output_price: Price,
    *,
    estimated_usage: Optional[TokenUsage] = None,
    estimation_source: Optional[CostEstimationSource] = None,
) -> CostComponent:
    """Calculate exact or estimated cost without changing ``ModelResponse``.

    Complete usage with ``provider_usage`` (or no source) is exact.  For a
    complete usage that came from a tokenizer/heuristic, pass that source
    explicitly.  Incomplete Provider usage must be accompanied by complete
    independently estimated usage and an explicit non-provider source.
    """

    if not isinstance(usage, TokenUsage):
        raise TypeError("usage must be a TokenUsage")

    if usage.is_complete:
        source = estimation_source or "provider_usage"
        estimated = source != "provider_usage"
        if estimated_usage is not None:
            raise ValueError(
                "estimated_usage is only valid when provider usage is incomplete"
            )
        return _component_from_complete_usage(
            usage,
            input_price,
            output_price,
            cost_estimated=estimated,
            source=source,
        )

    if estimated_usage is None or not estimated_usage.is_complete:
        raise ValueError(
            "incomplete usage requires complete estimated_usage and "
            "estimation_source"
        )
    if estimation_source not in {"local_tokenizer", "heuristic"}:
        raise ValueError(
            "incomplete usage requires estimation_source local_tokenizer "
            "or heuristic"
        )
    return _component_from_complete_usage(
        estimated_usage,
        input_price,
        output_price,
        cost_estimated=True,
        source=estimation_source,
    )


def calculate_cost_breakdown(
    *,
    classifier_cost: Optional[CostComponent] = None,
    execution_cost: Optional[CostComponent] = None,
    fallback_cost: Optional[CostComponent] = None,
) -> CostBreakdown:
    """Aggregate classifier, execution, and fallback costs deterministically."""

    components = {
        "classifier_cost": classifier_cost,
        "execution_cost": execution_cost,
        "fallback_cost": fallback_cost,
    }
    for name, component in components.items():
        if component is not None and not isinstance(component, CostComponent):
            raise TypeError(f"{name} must be a CostComponent or None")

    amounts = {
        name: _decimal_amount(component.cost if component else 0.0)
        for name, component in components.items()
    }
    estimated_sources = {
        component.cost_estimation_source
        for component in components.values()
        if component is not None and component.cost_estimated
    }
    if len(estimated_sources) > 1:
        raise ValueError(
            "multiple cost estimation sources cannot be represented by "
            "one CostBreakdown"
        )

    if estimated_sources:
        source = next(iter(estimated_sources))
        estimated = True
    elif any(
        component is not None
        and component.cost_estimation_source == "provider_usage"
        for component in components.values()
    ):
        source = "provider_usage"
        estimated = False
    else:
        source = None
        estimated = False

    total = sum(amounts.values(), Decimal("0"))
    return CostBreakdown(
        classifier_cost=float(amounts["classifier_cost"]),
        execution_cost=float(amounts["execution_cost"]),
        fallback_cost=float(amounts["fallback_cost"]),
        total_production_cost=float(total),
        cost_estimated=estimated,
        cost_estimation_source=source,
    )


def _component_from_complete_usage(
    usage: TokenUsage,
    input_price: Price,
    output_price: Price,
    *,
    cost_estimated: bool,
    source: Literal["provider_usage", "local_tokenizer", "heuristic"],
) -> CostComponent:
    assert usage.input_tokens is not None
    assert usage.output_tokens is not None
    return CostComponent(
        cost=calculate_model_cost(
            usage.input_tokens,
            usage.output_tokens,
            input_price,
            output_price,
        ),
        cost_estimated=cost_estimated,
        cost_estimation_source=source,
    )


def _validate_token_count(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _validate_price(value: Price, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be a non-negative number")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not decimal_value.is_finite():
        raise ValueError(f"{name} must be a finite number")
    if decimal_value < 0:
        raise ValueError(f"{name} must be non-negative")
    return decimal_value


def _decimal_amount(value: float) -> Decimal:
    return _validate_price(value, "cost")
