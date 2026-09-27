"""Deterministic T-13 ``task_010`` failure-injection seam.

This module builds only local Router components and the existing
``MockProvider``.  It intentionally exposes a safe summary instead of any
provider response, request, credential, or environment data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Union

from jev_router.cost import CostComponent
from jev_router.providers import MockProvider, MockScenario, ModelResponse
from jev_router.registry import ModelDefinition, ModelRegistry
from jev_router.router import Router
from jev_router.schemas import ClassifierResult, RouteRequest, RouteResult, TaskType


class FailureInjectionCase(str, Enum):
    """The two documented T-13 scenarios; no other case is supported."""

    FALLBACK_SUCCESS = "case_a_fallback_success"
    BUDGET_BLOCK = "case_b_budget_block"


class UnsupportedFailureCase(ValueError):
    """Stable local error raised before any Router or Provider call."""

    code = "unsupported_failure_case"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: unsupported case")


@dataclass(frozen=True)
class FailureInjectionRun:
    """Final Router result plus fresh local providers for call-count checks."""

    case: FailureInjectionCase
    result: RouteResult
    primary: MockProvider
    fallback_1: MockProvider
    fallback_2: Optional[MockProvider] = None

    @property
    def provider_call_counts(self) -> Dict[str, int]:
        """Return only deterministic invocation counts, never provider payloads."""

        counts = {
            "primary": self.primary.call_count,
            "fallback_1": self.fallback_1.call_count,
        }
        if self.fallback_2 is not None:
            counts["fallback_2"] = self.fallback_2.call_count
        return counts

    @property
    def fallback_reason(self) -> Optional[str]:
        """Derive the T-08 reason semantics from the public Router history."""

        history = self.result.fallback_history
        if len(history) < 2:
            return None
        if self.result.status == "failed":
            return history[-1].split(":", 1)[1]
        return history[0].split(":", 1)[1]

    def to_summary(self) -> Dict[str, Any]:
        """Export a JSON-safe result without arbitrary Provider output."""

        return {
            "case": self.case.value,
            "result": self.result.model_dump(mode="json"),
            "fallback_reason": self.fallback_reason,
            "provider_call_counts": self.provider_call_counts,
        }

    def to_summary_json(self) -> str:
        """Serialize :meth:`to_summary` with finite, deterministic JSON only."""

        return json.dumps(
            self.to_summary(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def run_failure_injection(
    case: Union[FailureInjectionCase, str],
) -> FailureInjectionRun:
    """Run one documented T-13 case through the complete Router contract.

    Every invocation creates fresh Providers and a fresh Router.  Invalid case
    names fail closed before any Provider is constructed or invoked.
    """

    selected_case = _coerce_case(case)
    if selected_case is FailureInjectionCase.FALLBACK_SUCCESS:
        return _run_fallback_success()
    return _run_budget_block()


def _run_fallback_success() -> FailureInjectionRun:
    primary = MockProvider([MockScenario.UNAVAILABLE])
    fallback_1 = MockProvider(
        [
            ModelResponse(
                text="fallback response sk-do-not-return",
                input_tokens=1000,
                output_tokens=500,
                provider_request_id="task-010-fallback",
            )
        ]
    )
    router = _router(
        providers={"primary": primary, "fallback_1": fallback_1},
        estimates={"primary": 0.10, "fallback_1": 0.20},
        classifier_cost=0.01,
    )
    result = router.route(
        RouteRequest(
            task_id="task_010",
            prompt="run the controlled fallback case",
            budget_limit=1.00,
        )
    )
    return FailureInjectionRun(
        case=FailureInjectionCase.FALLBACK_SUCCESS,
        result=result,
        primary=primary,
        fallback_1=fallback_1,
    )


def _run_budget_block() -> FailureInjectionRun:
    primary = MockProvider([MockScenario.TIMEOUT])
    fallback_1 = MockProvider([MockScenario.SUCCESS])
    fallback_2 = MockProvider([MockScenario.SUCCESS])
    router = _router(
        providers={
            "primary": primary,
            "fallback_1": fallback_1,
            "fallback_2": fallback_2,
        },
        estimates={
            "primary": 0.01,
            "fallback_1": 0.10,
            "fallback_2": 0.20,
        },
        classifier_cost=0.04,
    )
    result = router.route(
        RouteRequest(
            task_id="task_010",
            prompt="run the controlled budget-block case",
            budget_limit=0.05,
        )
    )
    return FailureInjectionRun(
        case=FailureInjectionCase.BUDGET_BLOCK,
        result=result,
        primary=primary,
        fallback_1=fallback_1,
        fallback_2=fallback_2,
    )


def _router(
    *,
    providers: Dict[str, MockProvider],
    estimates: Dict[str, float],
    classifier_cost: float,
) -> Router:
    models = {
        "primary": _model("actual-primary", "medium", 1),
        "fallback_1": _model("actual-fallback-1", "high", 2),
    }
    if "fallback_2" in providers:
        models["fallback_2"] = _model("actual-fallback-2", "high", 3)

    return Router(
        ModelRegistry(models),
        providers,
        lambda request: _classifier(),
        safe_default_model="primary",
        estimated_max_costs=estimates,
        classifier_cost_resolver=lambda request, value: CostComponent(
            cost=classifier_cost,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        ),
    )


def _model(
    model_id: str,
    capability: str,
    price: int,
) -> ModelDefinition:
    return ModelDefinition(
        provider="mock",
        model_id=model_id,
        capability=capability,
        task_types=[TaskType.CODING],
        input_cost_per_million=price,
        output_cost_per_million=price,
        enabled=True,
    )


def _classifier() -> ClassifierResult:
    return ClassifierResult(
        task_type=TaskType.CODING,
        difficulty_score=5,
        difficulty_bucket="medium",
        required_capability="medium",
        confidence=0.88,
        risk_level="medium",
    )


def _coerce_case(case: Union[FailureInjectionCase, str]) -> FailureInjectionCase:
    try:
        if isinstance(case, FailureInjectionCase):
            return case
        return FailureInjectionCase(case)
    except (TypeError, ValueError):
        raise UnsupportedFailureCase() from None
