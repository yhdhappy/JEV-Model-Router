"""Bounded JEV and execution-model fallback primitives for T-08.

This module deliberately does not choose a model or assemble a RouteResult.
It only returns auditable signals and execution evidence for the future Router
Core.  Budget inputs are numeric values supplied by the caller; token pricing
and usage prediction remain in :mod:`jev_router.cost` and the caller's policy.
"""

from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any, Callable, List, Literal, Mapping, Optional, Sequence, Union

from pydantic import Field

from .cost import CostComponent
from .errors import SchemaValidationError
from .providers import (
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderTimeoutError,
)
from .schemas import StrictModel


BudgetNumber = Union[int, float, Decimal]
EstimatedCost = Union[
    BudgetNumber,
    Mapping[str, BudgetNumber],
    Callable[[str], BudgetNumber],
]
ProviderSource = Union[
    ModelProvider,
    Mapping[str, ModelProvider],
    Callable[[str], ModelProvider],
]


class BudgetDecision(StrictModel):
    """The result of one pre-invocation budget check."""

    allowed: bool
    current_accumulated_cost: float = Field(ge=0)
    estimated_next_max_cost: float = Field(ge=0)
    budget_limit: Optional[float] = Field(default=None, ge=0)
    error_code: Optional[str] = None


class JEVFallbackResult(StrictModel):
    """A JEV success or an explicit safe-default signal.

    ``selected_model`` intentionally stays ``None`` on failure.  T-09 owns
    the eventual safe/default model decision.
    """

    status: Literal["success", "safe_default"]
    decision: Literal["jev_success", "safe_default_fallback"]
    value: Any = None
    fallback_reason: Optional[str] = None
    error_code: Optional[str] = None
    selected_model: Optional[str] = None


class AttemptRecord(StrictModel):
    """One bounded model attempt, including whether the Provider was called."""

    model: str = Field(min_length=1)
    status: Literal["success", "failed", "stopped"]
    called: bool
    budget_decision: BudgetDecision
    failure_reason: Optional[str] = None
    error_code: Optional[str] = None
    cost: Optional[CostComponent] = None


class FallbackResult(StrictModel):
    """Auditable result of a finite primary/fallback candidate sequence."""

    status: Literal["success", "failed", "stopped"]
    selected_model: Optional[str] = None
    response: Optional[ModelResponse] = None
    primary_failure: Optional[AttemptRecord] = None
    fallback_history: List[AttemptRecord] = Field(default_factory=list)
    fallback_reason: Optional[str] = None
    error_code: Optional[str] = None
    fallback_cost: CostComponent = Field(
        default_factory=lambda: CostComponent(cost=0.0)
    )
    current_accumulated_cost: float = Field(ge=0)


class ManualModelResult(StrictModel):
    """Result for one explicitly requested model, with no substitution path."""

    status: str
    selected_model: Optional[str] = None
    response: Optional[ModelResponse] = None
    cost: CostComponent = Field(default_factory=lambda: CostComponent(cost=0.0))
    budget_decision: BudgetDecision
    error_code: Optional[str] = None
    fallback_reason: Optional[str] = None
    replacement_model: Optional[str] = None


_JEV_REASON_BY_KIND = {
    "timeout": "jev_timeout",
    "network_error": "jev_network_error",
    "invalid_json": "jev_invalid_response",
    "invalid_response": "jev_invalid_response",
    "schema_error": "jev_schema_error",
    "provider_error": "jev_provider_error",
    "provider_timeout": "jev_timeout",
}
_JEV_REASONS = set(_JEV_REASON_BY_KIND.values())


def normalize_jev_failure(failure: object) -> str:
    """Map JEV failure kinds/exceptions to stable, non-sensitive reason codes."""

    if isinstance(failure, str):
        value = failure.strip().lower()
        if value in _JEV_REASONS:
            return value
        if value in _JEV_REASON_BY_KIND:
            return _JEV_REASON_BY_KIND[value]

    if isinstance(failure, (TimeoutError, ProviderTimeoutError)):
        return "jev_timeout"
    if isinstance(failure, (ConnectionError,)):
        return "jev_network_error"
    if isinstance(failure, SchemaValidationError):
        message = str(failure).lower()
        if "not valid json" in message or "invalid json" in message:
            return "jev_invalid_response"
        return "jev_schema_error"
    if isinstance(failure, ProviderError):
        if getattr(failure, "code", None) == "provider_timeout":
            return "jev_timeout"
        return "jev_provider_error"

    code = getattr(failure, "code", None)
    if isinstance(code, str):
        normalized_code = _JEV_REASON_BY_KIND.get(code.lower())
        if normalized_code is not None:
            return normalized_code

    # An unclassified JEV exception is still a provider failure to callers;
    # raw exception text must not become part of the stable contract.
    return "jev_provider_error"


def safe_jev_fallback(failure: object) -> JEVFallbackResult:
    """Return only the conservative fallback signal required by T-08."""

    reason = normalize_jev_failure(failure)
    return JEVFallbackResult(
        status="safe_default",
        decision="safe_default_fallback",
        fallback_reason=reason,
        error_code=reason,
    )


def execute_jev_with_fallback(
    classify: Callable[[], Any],
) -> JEVFallbackResult:
    """Run an injected JEV call and convert failures to safe-default output."""

    if not callable(classify):
        raise TypeError("classify must be callable")
    try:
        value = classify()
    except Exception as exc:
        return safe_jev_fallback(exc)
    return JEVFallbackResult(
        status="success",
        decision="jev_success",
        value=value,
    )


def check_budget(
    current_accumulated_cost: BudgetNumber,
    estimated_next_max_cost: BudgetNumber,
    budget_limit: Optional[BudgetNumber],
) -> BudgetDecision:
    """Allow a call only when ``current + next <= limit``.

    ``None`` means no per-task limit was configured.  Decimal arithmetic keeps
    the equality boundary deterministic instead of depending on binary float
    rounding.
    """

    current = _validated_decimal(current_accumulated_cost, "current_accumulated_cost")
    estimated = _validated_decimal(estimated_next_max_cost, "estimated_next_max_cost")
    limit = (
        None
        if budget_limit is None
        else _validated_decimal(budget_limit, "budget_limit")
    )
    allowed = limit is None or current + estimated <= limit
    return BudgetDecision(
        allowed=allowed,
        current_accumulated_cost=float(current),
        estimated_next_max_cost=float(estimated),
        budget_limit=None if limit is None else float(limit),
        error_code=None if allowed else "budget_limit_reached",
    )


def normalize_execution_failure(failure: object) -> str:
    """Normalize Provider failures without exposing exception details."""

    code = getattr(failure, "code", None)
    if code in {"model_unavailable", "provider_timeout", "provider_error"}:
        return code
    return "provider_error"


def execute_model_fallback(
    *,
    candidates: Sequence[str],
    providers: ProviderSource,
    request: ModelRequest,
    current_accumulated_cost: BudgetNumber,
    estimated_next_max_cost: EstimatedCost,
    budget_limit: Optional[BudgetNumber],
    cost_by_model: Optional[Mapping[str, CostComponent]] = None,
    max_attempts: Optional[int] = None,
) -> FallbackResult:
    """Try a finite, de-duplicated candidate sequence under Budget Guard.

    ``cost_by_model`` contains actual cost evidence supplied by the caller,
    usually produced by T-07.  It is not inferred or repriced here.
    """

    if not isinstance(request, ModelRequest):
        raise TypeError("request must be a ModelRequest")
    unique_candidates = _unique_candidates(candidates)
    if not unique_candidates:
        raise ValueError("candidates must contain at least one model")
    if max_attempts is not None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be a positive integer or None")
        if max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer or None")
        unique_candidates = unique_candidates[:max_attempts]

    current = _validated_decimal(current_accumulated_cost, "current_accumulated_cost")
    history: List[AttemptRecord] = []
    first_failure: Optional[str] = None
    response: Optional[ModelResponse] = None
    selected_model: Optional[str] = None
    terminal_error: Optional[str] = None
    stopped = False

    for model in unique_candidates:
        estimated = _resolve_estimated_cost(estimated_next_max_cost, model)
        budget = check_budget(current, estimated, budget_limit)
        supplied_cost = _cost_for_model(cost_by_model, model)
        if not budget.allowed:
            history.append(
                AttemptRecord(
                    model=model,
                    status="stopped",
                    called=False,
                    budget_decision=budget,
                    failure_reason="budget_limit_reached",
                    error_code="budget_limit_reached",
                    cost=None,
                )
            )
            terminal_error = "budget_limit_reached"
            stopped = True
            break

        current += _cost_amount(supplied_cost)
        provider = _resolve_provider(providers, model)
        if provider is None:
            failure_reason = "model_unavailable"
            history.append(
                AttemptRecord(
                    model=model,
                    status="failed",
                    called=False,
                    budget_decision=budget,
                    failure_reason=failure_reason,
                    error_code=failure_reason,
                    cost=supplied_cost,
                )
            )
            if first_failure is None:
                first_failure = failure_reason
            terminal_error = failure_reason
            continue

        model_request = request.model_copy(update={"model_id": model})
        try:
            response = provider.invoke(model_request)
        except Exception as exc:
            failure_reason = normalize_execution_failure(exc)
            history.append(
                AttemptRecord(
                    model=model,
                    status="failed",
                    called=True,
                    budget_decision=budget,
                    failure_reason=failure_reason,
                    error_code=failure_reason,
                    cost=supplied_cost,
                )
            )
            if first_failure is None:
                first_failure = failure_reason
            terminal_error = failure_reason
            continue

        history.append(
            AttemptRecord(
                model=model,
                status="success",
                called=True,
                budget_decision=budget,
                cost=supplied_cost,
            )
        )
        selected_model = model
        terminal_error = None
        break

    primary_failure = next(
        (attempt for attempt in history if attempt.status == "failed"), None
    )
    fallback_cost = _aggregate_cost(
        attempt.cost for attempt in history[1:] if attempt.cost is not None
    )
    if selected_model is not None:
        status = "success"
    elif stopped:
        status = "stopped"
    else:
        status = "failed"
    return FallbackResult(
        status=status,
        selected_model=selected_model,
        response=response if selected_model is not None else None,
        primary_failure=primary_failure,
        fallback_history=history,
        fallback_reason=(
            "budget_limit_reached" if stopped else first_failure
        ),
        error_code=None if selected_model is not None else terminal_error,
        fallback_cost=fallback_cost,
        current_accumulated_cost=float(current),
    )


def execute_manual_model(
    *,
    model: str,
    provider: ModelProvider,
    request: ModelRequest,
    current_accumulated_cost: BudgetNumber,
    estimated_next_max_cost: BudgetNumber,
    budget_limit: Optional[BudgetNumber],
    cost: Optional[CostComponent] = None,
) -> ManualModelResult:
    """Execute exactly the requested model after one Budget Guard check."""

    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    if not isinstance(request, ModelRequest):
        raise TypeError("request must be a ModelRequest")
    if not hasattr(provider, "invoke"):
        raise TypeError("provider must implement invoke")

    budget = check_budget(
        current_accumulated_cost,
        estimated_next_max_cost,
        budget_limit,
    )
    if not budget.allowed:
        return ManualModelResult(
            status="stopped",
            budget_decision=budget,
            error_code="manual_model_budget_exceeded",
            fallback_reason="manual_model_budget_exceeded",
        )

    try:
        response = provider.invoke(request.model_copy(update={"model_id": model}))
    except Exception as exc:
        error_code = normalize_execution_failure(exc)
        return ManualModelResult(
            status="failed",
            cost=cost or CostComponent(cost=0.0),
            budget_decision=budget,
            error_code=error_code,
            fallback_reason=error_code,
        )
    return ManualModelResult(
        status="success",
        selected_model=model,
        response=response,
        cost=cost or CostComponent(cost=0.0),
        budget_decision=budget,
    )


def _unique_candidates(candidates: Sequence[str]) -> List[str]:
    if isinstance(candidates, (str, bytes)):
        raise TypeError("candidates must be a sequence of model names")
    unique: List[str] = []
    seen = set()
    for model in candidates:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("candidate model names must be non-empty strings")
        if model not in seen:
            seen.add(model)
            unique.append(model)
    return unique


def _resolve_estimated_cost(estimated: EstimatedCost, model: str) -> BudgetNumber:
    if isinstance(estimated, Mapping):
        if model not in estimated:
            raise ValueError(f"missing estimated_next_max_cost for model '{model}'")
        return estimated[model]
    if callable(estimated):
        return estimated(model)
    return estimated


def _resolve_provider(source: ProviderSource, model: str) -> Optional[ModelProvider]:
    if isinstance(source, Mapping):
        return source.get(model)
    if callable(source) and not hasattr(source, "invoke"):
        return source(model)
    return source


def _cost_for_model(
    costs: Optional[Mapping[str, CostComponent]], model: str
) -> Optional[CostComponent]:
    if costs is None:
        return None
    component = costs.get(model)
    if component is not None and not isinstance(component, CostComponent):
        raise TypeError("cost_by_model values must be CostComponent instances")
    return component


def _aggregate_cost(components) -> CostComponent:
    values = list(components)
    if not values:
        return CostComponent(cost=0.0)
    estimated_sources = {
        component.cost_estimation_source
        for component in values
        if component.cost_estimated
    }
    if len(estimated_sources) > 1:
        raise ValueError(
            "multiple cost estimation sources cannot be represented by one "
            "fallback cost"
        )
    amount = sum(Decimal(str(component.cost)) for component in values)
    if estimated_sources:
        return CostComponent(
            cost=float(amount),
            cost_estimated=True,
            cost_estimation_source=next(iter(estimated_sources)),
        )
    if any(
        component.cost_estimation_source == "provider_usage"
        for component in values
    ):
        return CostComponent(
            cost=float(amount),
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        )
    return CostComponent(cost=float(amount))


def _cost_amount(component: Optional[CostComponent]) -> Decimal:
    return Decimal("0") if component is None else Decimal(str(component.cost))


def _validated_decimal(value: BudgetNumber, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be a non-negative finite number")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a non-negative finite number") from None
    if not decimal_value.is_finite() or decimal_value < 0:
        raise ValueError(f"{name} must be a non-negative finite number")
    if not isfinite(float(decimal_value)):
        raise ValueError(f"{name} must be a non-negative finite number")
    return decimal_value
