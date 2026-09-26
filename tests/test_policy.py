"""T-06 Policy Engine contract tests."""

import pytest

from jev_router.errors import NoEligibleModelError
from jev_router.policy import PolicyDecision, PolicyEngine
from jev_router.registry import ModelDefinition, ModelRegistry
from jev_router.schemas import CapabilityLevel, ClassifierResult, TaskType


def make_model(
    name,
    *,
    capability=CapabilityLevel.LOW,
    task_types=(TaskType.CODING,),
    input_cost=0,
    output_cost=0,
    enabled=True,
):
    return ModelDefinition(
        provider="test-provider",
        model_id=name,
        capability=capability,
        task_types=list(task_types),
        input_cost_per_million=input_cost,
        output_cost_per_million=output_cost,
        enabled=enabled,
    )


def make_classifier(
    *, task_type=TaskType.CODING, required_capability=CapabilityLevel.LOW
):
    return ClassifierResult(
        task_type=task_type,
        difficulty_score=5,
        difficulty_bucket="medium",
        required_capability=required_capability,
        confidence=0.9,
    )


def make_registry(models):
    return ModelRegistry(models)


def test_policy_filters_task_type_capability_and_disabled_models_with_reasons():
    registry = make_registry(
        {
            "unsupported_model": make_model(
                "unsupported_model", task_types=(TaskType.REVIEW,)
            ),
            "insufficient_model": make_model(
                "insufficient_model", capability=CapabilityLevel.LOW
            ),
            "disabled_model": make_model(
                "disabled_model", capability=CapabilityLevel.HIGH, enabled=False
            ),
            "medium_model": make_model(
                "medium_model", capability=CapabilityLevel.MEDIUM, input_cost=1
            ),
            "high_model": make_model(
                "high_model", capability=CapabilityLevel.HIGH, input_cost=2
            ),
        }
    )

    decision = PolicyEngine(registry).decide(
        make_classifier(
            task_type=TaskType.CODING,
            required_capability=CapabilityLevel.MEDIUM,
        )
    )

    assert isinstance(decision, PolicyDecision)
    assert decision.primary_model == "medium_model"
    assert decision.fallback_models == ["high_model"]
    assert any(
        "unsupported_model" in reason and "task_type" in reason
        for reason in decision.reason
    )
    assert any(
        "insufficient_model" in reason and "capability" in reason
        for reason in decision.reason
    )
    assert any(
        "disabled_model" in reason and "enabled=false" in reason
        for reason in decision.reason
    )
    assert any(
        "medium_model" in reason and "eligible" in reason
        for reason in decision.reason
    )
    assert any("primary_model=medium_model" in reason for reason in decision.reason)


def test_policy_never_selects_capability_below_high_requirement():
    registry = make_registry(
        {
            "medium_model": make_model(
                "medium_model",
                capability=CapabilityLevel.MEDIUM,
                task_types=(TaskType.ARCHITECTURE,),
                input_cost=0,
            ),
            "high_model": make_model(
                "high_model",
                capability=CapabilityLevel.HIGH,
                task_types=(TaskType.ARCHITECTURE,),
                input_cost=4,
            ),
        }
    )

    decision = PolicyEngine(registry).decide(
        make_classifier(
            task_type=TaskType.ARCHITECTURE,
            required_capability=CapabilityLevel.HIGH,
        )
    )

    assert decision.primary_model == "high_model"
    assert decision.fallback_models == []
    assert any(
        "medium_model" in reason and "capability" in reason
        for reason in decision.reason
    )


def test_policy_orders_eligible_models_by_phase_one_cost_proxy_then_name():
    registry = make_registry(
        {
            "zeta_model": make_model(
                "zeta_model", input_cost=1, output_cost=2
            ),
            "beta_model": make_model(
                "beta_model", input_cost=0, output_cost=5
            ),
            "alpha_model": make_model(
                "alpha_model", input_cost=1, output_cost=2
            ),
        }
    )

    decision = PolicyEngine(registry).decide(make_classifier())

    assert decision.primary_model == "alpha_model"
    assert decision.fallback_models == ["zeta_model", "beta_model"]
    assert any(
        "phase-one" in reason or "phase 1" in reason
        for reason in decision.reason
    )
    assert any("tie-break=input,output,model_name" in reason for reason in decision.reason)


def test_policy_output_is_stable_when_registry_insertion_order_changes():
    models = {
        "zeta_model": make_model("zeta_model", input_cost=1, output_cost=2),
        "alpha_model": make_model("alpha_model", input_cost=1, output_cost=2),
        "beta_model": make_model("beta_model", input_cost=0, output_cost=5),
    }
    classifier = make_classifier()

    first = PolicyEngine(make_registry(models)).decide(classifier)
    second = PolicyEngine(make_registry(dict(reversed(list(models.items()))))).decide(
        classifier
    )

    assert first == second


def test_policy_raises_stable_domain_error_when_no_model_is_eligible():
    registry = make_registry(
        {
            "disabled_model": make_model(
                "disabled_model",
                capability=CapabilityLevel.HIGH,
                task_types=(TaskType.ARCHITECTURE,),
                enabled=False,
            ),
            "wrong_task_model": make_model(
                "wrong_task_model", task_types=(TaskType.REVIEW,)
            ),
        }
    )

    with pytest.raises(NoEligibleModelError) as exc_info:
        PolicyEngine(registry).decide(
            make_classifier(
                task_type=TaskType.ARCHITECTURE,
                required_capability=CapabilityLevel.HIGH,
            )
        )

    assert exc_info.value.code == "no_eligible_model"
    assert str(exc_info.value) == (
        "No eligible model for task_type=architecture, required_capability=high"
    )
    assert exc_info.value.reasons == (
        "disabled_model excluded: enabled=false",
        "wrong_task_model excluded: task_type=architecture unsupported",
    )
