"""Strict, side-effect-free loader for the frozen Pilot Adjust config."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Dict, Mapping, Tuple, Union

from benchmark.runner import _parse_yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "benchmark" / "pilot_adjust_config.yaml"
EXPECTED_AFFECTED_TASK_IDS = (
    "task_003",
    "task_004",
    "task_005",
    "task_006",
    "task_008",
)
EXPECTED_ESTIMATED_MAX_COSTS = MappingProxyType(
    {"low_model": 0.10, "medium_model": 0.25, "high_model": 1.00}
)
_SECRET_OR_RAW_TEXT = re.compile(
    r"(?i)(?:/users/|/home/|~/|\bapi[_-]?key\b|\bauthorization\b|"
    r"\bbearer\b|\b(?:secret|credential)[_-]?(?:path|value|ref)\b)"
)


class PilotAdjustConfigError(ValueError):
    """Raised when the frozen Adjust config is missing or unsafe."""

    code = "pilot_adjust_config_error"

    def __init__(self, detail: str):
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class PilotAdjustSource:
    official_config_path: str
    results_path: str
    summary_json_path: str
    summary_markdown_path: str
    expected_decision: str


@dataclass(frozen=True)
class PilotAdjustOutputs:
    adjust_results_path: str
    adjust_artifacts_dir: str
    adjust_summary_json_path: str
    adjust_summary_markdown_path: str


@dataclass(frozen=True)
class PilotAdjustConfig:
    schema_version: str
    phase: str
    scope: str
    affected_task_ids: Tuple[str, ...]
    provider_timeout_seconds: float
    budget_limit_per_adjust_attempt: float
    estimated_max_costs: Mapping[str, float]
    source: PilotAdjustSource
    outputs: PilotAdjustOutputs
    execution_enabled: bool
    preflight_only: bool


def load_pilot_adjust_config(
    path: Union[str, Path] = DEFAULT_CONFIG_PATH,
) -> PilotAdjustConfig:
    """Load and validate Adjust values without executing or writing anything."""

    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PilotAdjustConfigError(f"unable to read config '{config_path}'") from exc
    if _SECRET_OR_RAW_TEXT.search(raw_text):
        raise PilotAdjustConfigError("config contains a secret, credential, or raw provider value")
    try:
        raw = _parse_yaml(raw_text)
    except (TypeError, ValueError) as exc:
        raise PilotAdjustConfigError(f"invalid YAML in '{config_path}'") from exc
    if not isinstance(raw, dict):
        raise PilotAdjustConfigError("top level must be a mapping")

    top = _mapping(
        raw,
        "top level",
        {"schema_version", "adjust", "source_pilot", "budget", "outputs", "execution_gate"},
    )
    if _string(top["schema_version"], "schema_version") != "0.1":
        raise PilotAdjustConfigError("schema_version must be '0.1'")

    adjust = _mapping(top["adjust"], "adjust", {"phase", "scope", "affected_task_ids"})
    phase = _string(adjust["phase"], "adjust.phase")
    if phase != "PILOT_ADJUST_GATE":
        raise PilotAdjustConfigError("adjust.phase must be PILOT_ADJUST_GATE")
    scope = _string(adjust["scope"], "adjust.scope")
    if scope != "timeout_fallback_budget_only":
        raise PilotAdjustConfigError("adjust.scope is not the frozen phase-1 scope")
    affected_task_ids = _string_tuple(adjust["affected_task_ids"], "adjust.affected_task_ids")
    if affected_task_ids != EXPECTED_AFFECTED_TASK_IDS:
        raise PilotAdjustConfigError(
            "adjust.affected_task_ids must be exactly task_003, task_004, task_005, task_006, task_008"
        )

    source_raw = _mapping(
        top["source_pilot"],
        "source_pilot",
        {"official_config_path", "results_path", "summary_json_path", "summary_markdown_path", "expected_decision"},
    )
    source = PilotAdjustSource(
        official_config_path=_source_path(source_raw["official_config_path"], "source_pilot.official_config_path"),
        results_path=_source_path(source_raw["results_path"], "source_pilot.results_path"),
        summary_json_path=_source_path(source_raw["summary_json_path"], "source_pilot.summary_json_path"),
        summary_markdown_path=_source_path(source_raw["summary_markdown_path"], "source_pilot.summary_markdown_path"),
        expected_decision=_string(source_raw["expected_decision"], "source_pilot.expected_decision"),
    )
    if source.official_config_path != "benchmark/pilot_config.yaml":
        raise PilotAdjustConfigError("source_pilot.official_config_path must name the original config")
    if source.results_path != "benchmark/results/pilot_runs.jsonl":
        raise PilotAdjustConfigError("source_pilot.results_path must name the original results")
    if source.summary_json_path != "benchmark/results/pilot_summary.json":
        raise PilotAdjustConfigError("source_pilot.summary_json_path must name the original summary")
    if source.summary_markdown_path != "benchmark/results/pilot_summary.md":
        raise PilotAdjustConfigError("source_pilot.summary_markdown_path must name the original summary")
    if source.expected_decision != "adjust":
        raise PilotAdjustConfigError("source_pilot.expected_decision must be adjust")

    budget = _mapping(
        top["budget"],
        "budget",
        {"provider_timeout_seconds", "budget_limit_per_adjust_attempt", "estimated_max_costs"},
    )
    provider_timeout = _positive_finite_number(
        budget["provider_timeout_seconds"], "budget.provider_timeout_seconds"
    )
    if provider_timeout != 300.0:
        raise PilotAdjustConfigError("budget.provider_timeout_seconds must remain 300")
    budget_limit = _finite_number(
        budget["budget_limit_per_adjust_attempt"], "budget.budget_limit_per_adjust_attempt"
    )
    if budget_limit <= 0:
        raise PilotAdjustConfigError("budget.budget_limit_per_adjust_attempt must be greater than 0")
    if budget_limit != 1.50:
        raise PilotAdjustConfigError("budget.budget_limit_per_adjust_attempt must remain 1.50")
    estimated_max_costs = _number_mapping(budget["estimated_max_costs"], "budget.estimated_max_costs")
    if estimated_max_costs != dict(EXPECTED_ESTIMATED_MAX_COSTS):
        raise PilotAdjustConfigError("budget.estimated_max_costs must remain low=0.10, medium=0.25, high=1.00")
    if budget_limit <= max(estimated_max_costs.values()):
        raise PilotAdjustConfigError("Adjust budget must be strictly greater than every model estimate")

    outputs_raw = _mapping(
        top["outputs"],
        "outputs",
        {"adjust_results_path", "adjust_artifacts_dir", "adjust_summary_json_path", "adjust_summary_markdown_path"},
    )
    output_values = {
        name: _output_path(outputs_raw[name], f"outputs.{name}")
        for name in outputs_raw
    }
    if len(set(output_values.values())) != len(output_values):
        raise PilotAdjustConfigError("Adjust output paths must be distinct")
    expected_outputs = {
        "adjust_results_path": "benchmark/results/adjust_runs.jsonl",
        "adjust_artifacts_dir": "benchmark/results/adjust_artifacts",
        "adjust_summary_json_path": "benchmark/results/adjust_summary.json",
        "adjust_summary_markdown_path": "benchmark/results/adjust_summary.md",
    }
    if output_values != expected_outputs:
        raise PilotAdjustConfigError("Adjust outputs must remain on the four new frozen paths")
    outputs = PilotAdjustOutputs(**output_values)

    gate = _mapping(top["execution_gate"], "execution_gate", {"execution_enabled", "preflight_only"})
    if gate["execution_enabled"] is not False or gate["preflight_only"] is not True:
        raise PilotAdjustConfigError("phase 1 execution gate must remain preflight-only and disabled")

    return PilotAdjustConfig(
        schema_version="0.1",
        phase=phase,
        scope=scope,
        affected_task_ids=affected_task_ids,
        provider_timeout_seconds=provider_timeout,
        budget_limit_per_adjust_attempt=budget_limit,
        estimated_max_costs=MappingProxyType(dict(estimated_max_costs)),
        source=source,
        outputs=outputs,
        execution_enabled=False,
        preflight_only=True,
    )


def _mapping(value: Any, label: str, keys: set) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise PilotAdjustConfigError(f"{label} must be a mapping")
    unknown = set(value) - keys
    missing = keys - set(value)
    if unknown:
        raise PilotAdjustConfigError(f"{label} contains unknown keys: {sorted(unknown)}")
    if missing:
        raise PilotAdjustConfigError(f"{label} is missing keys: {sorted(missing)}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PilotAdjustConfigError(f"{label} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise PilotAdjustConfigError(f"{label} must be a list")
    return tuple(_string(item, f"{label} item") for item in value)


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotAdjustConfigError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise PilotAdjustConfigError(f"{label} must be finite and non-negative")
    return number


def _positive_finite_number(value: Any, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        raise PilotAdjustConfigError(f"{label} must be finite and positive")
    return number


def _number_mapping(value: Any, label: str) -> Dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise PilotAdjustConfigError(f"{label} must be a non-empty mapping")
    return {
        _string(name, f"{label} model name"): _finite_number(amount, f"{label}[{name}]")
        for name, amount in value.items()
    }


def _source_path(value: Any, label: str) -> str:
    path = _string(value, label)
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts or "\\" in path:
        raise PilotAdjustConfigError(f"{label} must be a relative path")
    return path


def _output_path(value: Any, label: str) -> str:
    path = _source_path(value, label)
    if not path.startswith("benchmark/results/"):
        raise PilotAdjustConfigError(f"{label} must stay within benchmark/results")
    return path


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "EXPECTED_AFFECTED_TASK_IDS",
    "EXPECTED_ESTIMATED_MAX_COSTS",
    "PilotAdjustConfig",
    "PilotAdjustConfigError",
    "PilotAdjustOutputs",
    "PilotAdjustSource",
    "load_pilot_adjust_config",
]
