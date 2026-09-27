"""Deterministic model eligibility and ordering policy for T-06."""

from typing import List, Optional, Tuple

from pydantic import Field

from .errors import NoEligibleModelError
from .registry import ModelDefinition, ModelRegistry
from .schemas import CapabilityLevel, ClassifierResult, StrictModel


_CAPABILITY_RANK = {
    CapabilityLevel.LOW: 0,
    CapabilityLevel.MEDIUM: 1,
    CapabilityLevel.HIGH: 2,
}


class PolicyDecision(StrictModel):
    """Auditable model order produced without executing a Provider."""

    primary_model: str = Field(min_length=1)
    fallback_models: List[str] = Field(default_factory=list)
    reason: List[str] = Field(min_length=1)


PolicyResult = PolicyDecision


class PolicyEngine:
    """Filter configured models, then order eligible models deterministically."""

    def __init__(self, registry: ModelRegistry):
        if not isinstance(registry, ModelRegistry):
            raise TypeError("registry must be a ModelRegistry")
        self._registry = registry

    def decide(self, classification: ClassifierResult) -> PolicyDecision:
        """Return primary and fallback models for one classifier result.

        T-06 uses a phase-one price proxy only: the sum of configured input
        and output prices per million tokens.  It is not T-07 token-cost
        accounting and does not execute, budget, or measure any Provider.
        """

        if not isinstance(classification, ClassifierResult):
            raise TypeError("classification must be a ClassifierResult")

        task_type = classification.task_type.value
        required_capability = classification.required_capability
        reasons = [
            f"task_type={task_type}",
            f"required_capability={required_capability.value}",
        ]
        eligible: List[Tuple[str, ModelDefinition]] = []
        exclusion_reasons: List[str] = []

        for name, model in sorted(self._registry.models.items()):
            exclusion = _exclusion_reason(name, model, classification)
            if exclusion is not None:
                reasons.append(exclusion)
                exclusion_reasons.append(exclusion)
                continue

            eligible.append((name, model))
            reasons.append(
                f"{name} eligible: capability={model.capability.value}, "
                f"phase-one cost proxy={_price_proxy(model):g} "
                f"(input={model.input_cost_per_million:g}, "
                f"output={model.output_cost_per_million:g}; "
                "tie-break=input,output,model_name)"
            )

        if not eligible:
            raise NoEligibleModelError(
                f"No eligible model for task_type={task_type}, "
                f"required_capability={required_capability.value}",
                reasons=exclusion_reasons,
            )

        ordered = sorted(eligible, key=lambda item: _sort_key(item[0], item[1]))
        primary_model = ordered[0][0]
        fallback_models = [name for name, _ in ordered[1:]]
        reasons.append(
            f"primary_model={primary_model} selected by lowest phase-one cost "
            "proxy, then input/output price and model name"
        )
        if fallback_models:
            reasons.append(
                "fallback_models="
                + ",".join(fallback_models)
                + " ordered by ascending phase-one cost proxy"
            )
        else:
            reasons.append("fallback_models=[]: no additional eligible models")

        return PolicyDecision(
            primary_model=primary_model,
            fallback_models=fallback_models,
            reason=reasons,
        )

    select = decide


def _exclusion_reason(
    name: str, model: ModelDefinition, classification: ClassifierResult
) -> Optional[str]:
    if classification.task_type not in model.task_types:
        return (
            f"{name} excluded: task_type={classification.task_type.value} "
            "unsupported"
        )
    if _CAPABILITY_RANK[model.capability] < _CAPABILITY_RANK[
        classification.required_capability
    ]:
        return (
            f"{name} excluded: capability={model.capability.value} below "
            f"required_capability={classification.required_capability.value}"
        )
    if not model.enabled:
        return f"{name} excluded: enabled=false"
    return None


def _price_proxy(model: ModelDefinition) -> float:
    return float(model.input_cost_per_million + model.output_cost_per_million)


def _sort_key(name: str, model: ModelDefinition) -> Tuple[float, float, float, str]:
    return (
        _price_proxy(model),
        float(model.input_cost_per_million),
        float(model.output_cost_per_million),
        name,
    )
