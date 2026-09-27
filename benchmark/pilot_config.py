"""Strict, side-effect-free loader for the frozen official Pilot config.

Loading this module never constructs a Provider, reads credentials, starts a
task, or writes a result.  The official runner must separately opt into an
explicit execution mode after this configuration has been reviewed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple, Union

from benchmark.runner import _parse_yaml
from jev_router.errors import ConfigurationError
from jev_router.registry import ModelRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "benchmark" / "pilot_config.yaml"
DEFAULT_MODEL_REGISTRY_PATH = PROJECT_ROOT / "config" / "models.real-pilot.yaml"
EXPECTED_REAL_TASK_IDS = tuple(f"task_{number:03d}" for number in range(1, 10))
CONTROLLED_FAILURE_TASK_ID = "task_010_fallback"
ALLOWED_AUDIT_ASSESSMENTS = frozenset(
    {"reasonable", "questionable", "clearly_unreasonable", "not_applicable"}
)
EXPECTED_REPEAT_TRIGGERS = (
    "router_selected_model_failed_and_adjacent_stronger_passed",
    "jev_confidence_below",
    "difficulty_score_near_bucket_boundary",
    "human_jev_assessment",
    "unexpected_fallback",
    "result_material_to_go_adjust_stop",
)
EXPECTED_FORBIDDEN_REPEAT_TRIGGERS = (
    "result_dislike",
    "ad_hoc_rerun",
)
_OUTPUT_FIELDS = (
    "official_results_path",
    "summary_json_path",
    "summary_markdown_path",
    "artifacts_dir",
)
_SECRET_OR_RAW_TEXT = re.compile(
    r"(?i)(?:/users/|/home/|~/|\bapi[_-]?key\b|\bauthorization\b|"
    r"\bbearer\b|\braw[_ -]?provider\b|\bprovider[_ -]?raw\b|"
    r"\b(?:secret|credential)[_-]?(?:path|value|ref)\b|\bsk-[a-z0-9])"
)


class PilotConfigError(ValueError):
    """Raised when the official Pilot config is missing or unsafe."""

    code = "pilot_config_error"

    def __init__(self, detail: str):
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class PilotOutputs:
    official_results_path: str
    summary_json_path: str
    summary_markdown_path: str
    artifacts_dir: str


@dataclass(frozen=True)
class PilotConfig:
    schema_version: str
    total_task_slots: int
    real_task_ids: Tuple[str, ...]
    controlled_failure_task_id: str
    baseline_model: str
    baseline_rationale: str
    budget_limit_per_real_task: float
    estimated_max_costs: Mapping[str, float]
    budget_contract: str
    max_extra_runs_per_task: int
    jev_confidence_threshold: float
    difficulty_boundary_scores: Tuple[int, ...]
    repeat_triggers: Tuple[str, ...]
    forbidden_repeat_triggers: Tuple[str, ...]
    audit_assessments: Tuple[str, ...]
    audit_after_lightweight_rule: bool
    audit_timing: str
    audit_cost_field: str
    audit_cost_included_in_production_route_cost: bool
    controlled_failure_audit_assessment: str
    controlled_failure_audit_reason: str
    outputs: PilotOutputs
    outcome: Optional[str]
    go_criteria: Tuple[str, ...]
    adjust_criteria: Tuple[str, ...]
    stop_criteria: Tuple[str, ...]


def load_pilot_config(
    path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    *,
    model_registry_path: Union[str, Path] = DEFAULT_MODEL_REGISTRY_PATH,
) -> PilotConfig:
    """Load and validate the frozen config without any execution side effect."""

    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PilotConfigError(f"unable to read config '{config_path}'") from exc
    if _SECRET_OR_RAW_TEXT.search(raw_text):
        raise PilotConfigError("config contains a secret, credential, home path, or raw provider value")
    try:
        raw = _parse_yaml(raw_text)
    except (TypeError, ValueError) as exc:
        raise PilotConfigError(f"invalid YAML in '{config_path}'") from exc
    if not isinstance(raw, dict):
        raise PilotConfigError("top level must be a mapping")

    top = _mapping(
        raw,
        "top level",
        {
            "schema_version",
            "pilot",
            "budget",
            "repeat_policy",
            "jev_audit",
            "outputs",
            "decision",
        },
    )
    schema_version = _string(top["schema_version"], "schema_version")
    if schema_version != "0.1":
        raise PilotConfigError("schema_version must be '0.1'")

    pilot = _mapping(
        top["pilot"],
        "pilot",
        {
            "total_task_slots",
            "real_task_ids",
            "controlled_failure_task_id",
            "baseline_model",
            "baseline_rationale",
            "controlled_failure_mode",
        },
    )
    total_slots = _int(pilot["total_task_slots"], "pilot.total_task_slots")
    real_task_ids = _string_tuple(pilot["real_task_ids"], "pilot.real_task_ids")
    if real_task_ids != EXPECTED_REAL_TASK_IDS or len(set(real_task_ids)) != 9:
        raise PilotConfigError("pilot.real_task_ids must be exactly task_001 through task_009")
    controlled_task_id = _string(
        pilot["controlled_failure_task_id"], "pilot.controlled_failure_task_id"
    )
    if controlled_task_id != CONTROLLED_FAILURE_TASK_ID:
        raise PilotConfigError("pilot.controlled_failure_task_id must be task_010_fallback")
    if total_slots != len(real_task_ids) + 1:
        raise PilotConfigError("pilot.total_task_slots must equal the 9 real tasks plus the controlled task")
    baseline_model = _string(pilot["baseline_model"], "pilot.baseline_model")
    baseline_rationale = _string(pilot["baseline_rationale"], "pilot.baseline_rationale")
    if _string(pilot["controlled_failure_mode"], "pilot.controlled_failure_mode") != "controlled_mock_only":
        raise PilotConfigError("task_010_fallback must be controlled_mock_only")

    budget = _mapping(
        top["budget"],
        "budget",
        {"budget_limit_per_real_task", "estimated_max_costs", "contract"},
    )
    budget_limit = _finite_number(
        budget["budget_limit_per_real_task"], "budget.budget_limit_per_real_task"
    )
    if budget_limit <= 0:
        raise PilotConfigError("budget.budget_limit_per_real_task must be greater than 0")
    contract = _string(budget["contract"], "budget.contract")
    if contract != "per_attempt":
        raise PilotConfigError("budget.contract must be per_attempt")
    registry = _load_registry(model_registry_path)
    enabled_models = set(registry.enabled_names())
    if baseline_model not in enabled_models:
        raise PilotConfigError("pilot.baseline_model must be enabled in the real-pilot registry")
    estimated_costs = _number_mapping(budget["estimated_max_costs"], "budget.estimated_max_costs")
    unknown_cost_models = set(estimated_costs) - set(registry.names())
    if unknown_cost_models:
        raise PilotConfigError("estimated_max_costs contains unknown model names")
    missing_cost_models = enabled_models - set(estimated_costs)
    if missing_cost_models:
        raise PilotConfigError("estimated_max_costs is missing an enabled model")
    if any(estimated_costs[name] >= budget_limit for name in enabled_models):
        raise PilotConfigError(
            "budget limit must be strictly greater than every enabled model estimate"
        )

    repeat = _mapping(
        top["repeat_policy"],
        "repeat_policy",
        {
            "max_extra_runs_per_task",
            "jev_confidence_threshold",
            "difficulty_boundary_scores",
            "triggers",
            "forbidden_triggers",
        },
    )
    max_extra_runs = _int(repeat["max_extra_runs_per_task"], "repeat_policy.max_extra_runs_per_task")
    if max_extra_runs != 2:
        raise PilotConfigError("repeat_policy.max_extra_runs_per_task must be exactly 2")
    confidence_threshold = _finite_number(
        repeat["jev_confidence_threshold"], "repeat_policy.jev_confidence_threshold"
    )
    if not 0 <= confidence_threshold <= 1:
        raise PilotConfigError("repeat_policy.jev_confidence_threshold must be between 0 and 1")
    boundary_scores = _int_tuple(
        repeat["difficulty_boundary_scores"], "repeat_policy.difficulty_boundary_scores"
    )
    if boundary_scores != (3, 4, 7, 8) or any(score < 1 or score > 10 for score in boundary_scores):
        raise PilotConfigError("difficulty boundary scores must be exactly [3, 4, 7, 8]")
    repeat_triggers = _string_tuple(repeat["triggers"], "repeat_policy.triggers")
    if repeat_triggers != EXPECTED_REPEAT_TRIGGERS:
        raise PilotConfigError("repeat_policy.triggers do not match the frozen trigger list")
    forbidden_triggers = _string_tuple(repeat["forbidden_triggers"], "repeat_policy.forbidden_triggers")
    if forbidden_triggers != EXPECTED_FORBIDDEN_REPEAT_TRIGGERS:
        raise PilotConfigError("repeat_policy forbids only result-dislike and ad-hoc reruns")

    audit = _mapping(
        top["jev_audit"],
        "jev_audit",
        {
            "assessment_enum",
            "record_when_classifier_available",
            "audit_after_lightweight_rule",
            "audit_timing",
            "audit_cost_field",
            "audit_cost_included_in_production_route_cost",
            "controlled_failure",
        },
    )
    audit_assessments = _string_tuple(audit["assessment_enum"], "jev_audit.assessment_enum")
    if set(audit_assessments) != ALLOWED_AUDIT_ASSESSMENTS or len(audit_assessments) != 4:
        raise PilotConfigError("jev_audit.assessment_enum contains an unsupported value")
    if audit["record_when_classifier_available"] is not True:
        raise PilotConfigError("JEV assessment must be recorded when a classifier result is available")
    audit_after_lightweight_rule = audit["audit_after_lightweight_rule"]
    if not isinstance(audit_after_lightweight_rule, bool) or not audit_after_lightweight_rule:
        raise PilotConfigError("lightweight-rule routes require an audit-only JEV classification")
    audit_timing = _string(audit["audit_timing"], "jev_audit.audit_timing")
    if audit_timing != "after_execution":
        raise PilotConfigError("lightweight-rule JEV audit must run after_execution")
    audit_cost_field = _string(audit["audit_cost_field"], "jev_audit.audit_cost_field")
    if audit_cost_field != "experimental_validation_cost":
        raise PilotConfigError("JEV audit cost must be experimental_validation_cost")
    if audit["audit_cost_included_in_production_route_cost"] is not False:
        raise PilotConfigError("experimental validation cost must not be included in production route cost")
    controlled_audit = _mapping(
        audit["controlled_failure"],
        "jev_audit.controlled_failure",
        {"assessment", "reason"},
    )
    controlled_audit_assessment = _string(
        controlled_audit["assessment"], "jev_audit.controlled_failure.assessment"
    )
    if controlled_audit_assessment not in ALLOWED_AUDIT_ASSESSMENTS:
        raise PilotConfigError("controlled failure audit assessment is not an allowed enum value")
    controlled_audit_reason = _string(
        controlled_audit["reason"], "jev_audit.controlled_failure.reason"
    )
    if controlled_audit_assessment == "not_applicable" and controlled_audit_reason != "controlled_mock":
        raise PilotConfigError("not_applicable controlled failure audit must state controlled_mock")

    outputs_raw = _mapping(top["outputs"], "outputs", set(_OUTPUT_FIELDS))
    output_values = {field: _output_path(outputs_raw[field], f"outputs.{field}") for field in _OUTPUT_FIELDS}
    if len(set(output_values.values())) != len(output_values):
        raise PilotConfigError("official output paths must be distinct")
    outputs = PilotOutputs(**output_values)

    decision = _mapping(
        top["decision"],
        "decision",
        {"outcome", "go_criteria", "adjust_criteria", "stop_criteria"},
    )
    outcome = decision["outcome"]
    if outcome is not None and outcome != "pending":
        raise PilotConfigError("decision.outcome must be null or pending before the Pilot")
    go_criteria = _criteria(decision["go_criteria"], "decision.go_criteria")
    adjust_criteria = _criteria(decision["adjust_criteria"], "decision.adjust_criteria")
    stop_criteria = _criteria(decision["stop_criteria"], "decision.stop_criteria")

    return PilotConfig(
        schema_version=schema_version,
        total_task_slots=total_slots,
        real_task_ids=real_task_ids,
        controlled_failure_task_id=controlled_task_id,
        baseline_model=baseline_model,
        baseline_rationale=baseline_rationale,
        budget_limit_per_real_task=budget_limit,
        estimated_max_costs=MappingProxyType(dict(estimated_costs)),
        budget_contract=contract,
        max_extra_runs_per_task=max_extra_runs,
        jev_confidence_threshold=confidence_threshold,
        difficulty_boundary_scores=boundary_scores,
        repeat_triggers=repeat_triggers,
        forbidden_repeat_triggers=forbidden_triggers,
        audit_assessments=audit_assessments,
        audit_after_lightweight_rule=audit_after_lightweight_rule,
        audit_timing=audit_timing,
        audit_cost_field=audit_cost_field,
        audit_cost_included_in_production_route_cost=False,
        controlled_failure_audit_assessment=controlled_audit_assessment,
        controlled_failure_audit_reason=controlled_audit_reason,
        outputs=outputs,
        outcome=outcome,
        go_criteria=go_criteria,
        adjust_criteria=adjust_criteria,
        stop_criteria=stop_criteria,
    )


def _load_registry(path: Union[str, Path]) -> ModelRegistry:
    try:
        return ModelRegistry.from_yaml(path)
    except (ConfigurationError, OSError, ValueError, KeyError) as exc:
        raise PilotConfigError("real-pilot model registry is invalid") from exc


def _mapping(value: Any, label: str, keys: set) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PilotConfigError(f"{label} must be a mapping")
    unknown = set(value) - keys
    missing = keys - set(value)
    if unknown:
        raise PilotConfigError(f"{label} contains unknown keys: {sorted(unknown)}")
    if missing:
        raise PilotConfigError(f"{label} is missing keys: {sorted(missing)}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PilotConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PilotConfigError(f"{label} must be an integer")
    return value


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotConfigError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise PilotConfigError(f"{label} must be finite and non-negative")
    return number


def _number_mapping(value: Any, label: str) -> Dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise PilotConfigError(f"{label} must be a non-empty mapping")
    result: Dict[str, float] = {}
    for name, amount in value.items():
        result[_string(name, f"{label} model name")] = _finite_number(amount, f"{label}[{name}]")
    return result


def _string_tuple(value: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise PilotConfigError(f"{label} must be a list")
    return tuple(_string(item, f"{label} item") for item in value)


def _int_tuple(value: Any, label: str) -> Tuple[int, ...]:
    if not isinstance(value, list):
        raise PilotConfigError(f"{label} must be a list")
    return tuple(_int(item, f"{label} item") for item in value)


def _criteria(value: Any, label: str) -> Tuple[str, ...]:
    criteria = _string_tuple(value, label)
    if not criteria:
        raise PilotConfigError(f"{label} must not be empty")
    return criteria


def _output_path(value: Any, label: str) -> str:
    path = _string(value, label)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path:
        raise PilotConfigError(f"{label} must be a relative benchmark/results path")
    if not path.startswith("benchmark/results/"):
        raise PilotConfigError(f"{label} must stay within benchmark/results")
    return path


__all__ = [
    "ALLOWED_AUDIT_ASSESSMENTS",
    "CONTROLLED_FAILURE_TASK_ID",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_MODEL_REGISTRY_PATH",
    "EXPECTED_REAL_TASK_IDS",
    "PilotConfig",
    "PilotConfigError",
    "PilotOutputs",
    "load_pilot_config",
]
