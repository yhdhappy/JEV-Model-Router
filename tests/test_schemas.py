"""T-01 schema contract tests."""

import pytest
from pydantic import ValidationError

from jev_router.schemas import (
    CapabilityLevel,
    ClassifierResult,
    CostBreakdown,
    DifficultyBucket,
    RouteRequest,
    RouteResult,
    TaskType,
)


def make_classifier(**overrides):
    payload = {
        "schema_version": "0.1",
        "task_type": "debugging",
        "difficulty_score": 6,
        "difficulty_bucket": "medium",
        "required_capability": "medium",
        "confidence": 0.86,
        "risk_level": "medium",
        "notes": "multi-file debugging",
    }
    payload.update(overrides)
    return ClassifierResult.model_validate(payload)


def test_route_request_valid():
    request = RouteRequest(
        task_id="task_001",
        prompt="修复登录失败问题并运行测试",
        context=["README.md"],
        budget_limit=0.5,
        metadata={"source": "cli"},
    )

    assert request.task_id == "task_001"
    assert request.budget_limit == 0.5
    assert request.manual_model is None


def test_route_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RouteRequest.model_validate(
            {
                "task_id": "task_001",
                "prompt": "test",
                "unexpected": True,
            }
        )


@pytest.mark.parametrize("score", [0, 11])
def test_classifier_rejects_difficulty_out_of_range(score):
    with pytest.raises(ValidationError):
        make_classifier(difficulty_score=score)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_classifier_rejects_confidence_out_of_range(confidence):
    with pytest.raises(ValidationError):
        make_classifier(confidence=confidence)


def test_classifier_rejects_unknown_task_type():
    with pytest.raises(ValidationError):
        make_classifier(task_type="unknown_type")


def test_classifier_accepts_defined_enums():
    result = make_classifier()

    assert result.task_type is TaskType.DEBUGGING
    assert result.difficulty_bucket is DifficultyBucket.MEDIUM
    assert result.required_capability is CapabilityLevel.MEDIUM


def test_route_result_carries_cost_estimated_flag():
    result = RouteResult(
        task_id="task_001",
        status="success",
        route_source="jev",
        classifier=make_classifier(),
        selected_model="medium_model",
        cost=CostBreakdown(
            classifier_cost=0.001,
            execution_cost=0.02,
            total_production_cost=0.021,
            cost_estimated=True,
            cost_estimation_source="local_tokenizer",
        ),
    )

    assert result.cost.cost_estimated is True
    assert result.cost.cost_estimation_source == "local_tokenizer"


def test_route_result_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        RouteResult.model_validate(
            {
                "task_id": "task_001",
                "status": "failed",
                "route_source": "safe_default",
                "unknown": "not-allowed",
            }
        )
