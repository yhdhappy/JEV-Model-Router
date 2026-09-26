"""Minimal, local JSONL logging for terminal router results."""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Union

from .schemas import RouteResult


_REDACTED = "[REDACTED]"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*"
        r"[\"']?[^\s,;\"']+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{7,}\b"),
)


class JsonlLoggingStore:
    """Append safe projections of ``RouteResult`` values to a local JSONL file."""

    def __init__(self, path: Union[str, os.PathLike[str]]) -> None:
        self.path = Path(path)

    def append(self, result: RouteResult) -> None:
        if not isinstance(result, RouteResult):
            raise TypeError("result must be a RouteResult")

        record = _sanitize(_project(result))
        payload = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"

        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            stream = os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="",
            )
        except Exception:
            os.close(descriptor)
            raise
        try:
            stream.write(payload)
            stream.flush()
        finally:
            stream.close()

    def __call__(self, result: RouteResult) -> None:
        self.append(result)


def _project(result: RouteResult) -> Dict[str, Any]:
    classifier = result.classifier
    classifier_record: Any = None
    if classifier is not None:
        classifier_record = {
            "task_type": classifier.task_type.value,
            "difficulty_score": classifier.difficulty_score,
            "difficulty_bucket": classifier.difficulty_bucket.value,
            "required_capability": classifier.required_capability.value,
            "confidence": classifier.confidence,
            "risk_level": classifier.risk_level.value,
        }

    cost = result.cost
    return {
        "schema_version": "0.1",
        "task_id": result.task_id,
        "status": result.status,
        "route_source": result.route_source,
        "rule_id": result.rule_id,
        "classifier": classifier_record,
        "selected_model": result.selected_model,
        "jev_called": classifier is not None
        or result.route_source in {"jev", "safe_default"},
        "fallback_used": result.route_source == "fallback"
        or bool(result.fallback_history),
        "fallback_history": list(result.fallback_history),
        "classifier_cost": cost.classifier_cost,
        "execution_cost": cost.execution_cost,
        "fallback_cost": cost.fallback_cost,
        "total_production_cost": cost.total_production_cost,
        "cost_estimated": cost.cost_estimated,
        "cost_estimation_source": cost.cost_estimation_source,
        "error_codes": [error.code for error in result.errors],
    }


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    if isinstance(value, dict):
        return {
            _sanitize_string(key) if isinstance(key, str) else key: _sanitize(item)
            for key, item in value.items()
        }
    return value


def _sanitize_string(value: str) -> str:
    sanitized = value
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub(_REDACTED, sanitized)
    return sanitized
