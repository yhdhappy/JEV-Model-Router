"""Strict schemas shared by the stage-1 router components."""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects unknown fields to keep contracts explicit."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class TaskType(str, Enum):
    FILE_OPERATION = "file_operation"
    CODING = "coding"
    DEBUGGING = "debugging"
    TESTING = "testing"
    REVIEW = "review"
    ARCHITECTURE = "architecture"
    RESEARCH = "research"
    REASONING = "reasoning"
    OTHER = "other"


class DifficultyBucket(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CapabilityLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RouteRequest(StrictModel):
    """Input contract for one router decision/execution request."""

    task_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    context: List[str] = Field(default_factory=list)
    manual_model: Optional[str] = None
    budget_limit: Optional[float] = Field(default=None, ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ClassifierResult(StrictModel):
    """Normalized output expected from the JEV classifier."""

    schema_version: Literal["0.1"] = "0.1"
    task_type: TaskType
    difficulty_score: int = Field(ge=1, le=10)
    difficulty_bucket: DifficultyBucket
    required_capability: CapabilityLevel
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    notes: Optional[str] = None


class CostBreakdown(StrictModel):
    """Production-cost fields carried by every route result."""

    classifier_cost: float = Field(default=0.0, ge=0)
    execution_cost: float = Field(default=0.0, ge=0)
    fallback_cost: float = Field(default=0.0, ge=0)
    total_production_cost: float = Field(default=0.0, ge=0)
    cost_estimated: bool = False
    cost_estimation_source: Optional[
        Literal["provider_usage", "local_tokenizer", "heuristic"]
    ] = None


class RouteError(StrictModel):
    """Stable machine-readable error returned by the router."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class RouteResult(StrictModel):
    """Unified result contract for successful or failed route executions."""

    task_id: str = Field(min_length=1)
    status: Literal["success", "failed"]
    route_source: Literal[
        "manual_override",
        "light_rule",
        "jev",
        "fallback",
        "safe_default",
    ]
    rule_id: Optional[str] = None
    classifier: Optional[ClassifierResult] = None
    selected_model: Optional[str] = None
    fallback_history: List[str] = Field(default_factory=list)
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    acceptance: Optional[Dict[str, Any]] = None
    errors: List[RouteError] = Field(default_factory=list)
