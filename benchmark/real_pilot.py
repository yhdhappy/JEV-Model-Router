"""Opt-in real-provider Pilot runner wiring.

This module is deliberately separate from :mod:`benchmark.runner`.  The
generic harness owns fixture snapshots and acceptance; this bridge owns the
runtime-only Router/provider configuration and produces a small safe record.
It does not modify fixture files or replace their frozen placeholders.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from jev_router.opencode_go import OpenCodeGoProvider
from jev_router.providers import ModelResponse
from jev_router.real_jev import RealJEVClassifier
from jev_router.registry import ModelDefinition, ModelRegistry
from jev_router.router import Router
from jev_router.schemas import RouteRequest, RouteResult

from benchmark.runner import load_fixture, run_fixture


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "config" / "models.real-pilot.yaml"
OFFICIAL_RESULT_PATH = PROJECT_ROOT / "benchmark" / "results" / "pilot_runs.jsonl"
DEFAULT_GATE_SMOKE_RESULT_PATH = (
    PROJECT_ROOT / "benchmark" / "results" / "pilot_runner_gate_smoke.jsonl"
)
_OFFICIAL_APPEND_TOKEN = object()
MAX_EVIDENCE_RESPONSE_BYTES = 256 * 1024
MAX_EVIDENCE_FILE_BYTES = 256 * 1024
MAX_EVIDENCE_TOTAL_BYTES = 1024 * 1024


class RealPilotConfigurationError(ValueError):
    """Raised when runtime-only real Pilot configuration is unsafe or invalid."""

    code = "real_pilot_configuration_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


class ControlledMockFixtureError(ValueError):
    """Raised before any provider is built for the controlled Mock fixture."""

    code = "controlled_mock_fixture_not_real"

    def __init__(self) -> None:
        super().__init__(f"{self.code}: task_010_fallback is not a real-provider fixture")


class _BaselineJEVInvocationError(RuntimeError):
    """Stable internal failure if manual baseline routing ever calls JEV."""

    code = "baseline_jev_must_not_be_called"

    def __init__(self) -> None:
        super().__init__("Baseline JEV sentinel must not be called")


class _BaselineJEVSentinel:
    """Never constructs credentials and must never be reached by manual routing."""

    def classify(self, _request: RouteRequest) -> Any:
        raise _BaselineJEVInvocationError()

    def resolve_cost(self, _request: RouteRequest, _classifier: Any) -> Any:
        raise _BaselineJEVInvocationError()


@dataclass(frozen=True)
class RealPilotRuntimeConfig:
    """Runtime values that must not be written into a frozen fixture."""

    baseline_model: str
    budget_limit: float
    estimated_max_costs: Mapping[str, Union[int, float, Decimal]]
    provider_timeout_seconds: Union[int, float, Decimal] = 120.0
    registry_path: Path = DEFAULT_REGISTRY_PATH
    safe_default_model: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.baseline_model, str) or not self.baseline_model.strip():
            raise RealPilotConfigurationError("baseline_model must be non-empty")
        _validate_finite_non_negative(self.budget_limit, "budget_limit")
        _validate_positive_finite(self.provider_timeout_seconds, "provider_timeout_seconds")
        if not isinstance(self.estimated_max_costs, Mapping):
            raise RealPilotConfigurationError("estimated_max_costs must be a mapping")
        normalized: Dict[str, Union[int, float, Decimal]] = {}
        for name, value in self.estimated_max_costs.items():
            if not isinstance(name, str) or not name.strip():
                raise RealPilotConfigurationError("cost estimate names must be non-empty")
            _validate_finite_non_negative(value, f"estimated_max_costs[{name}]")
            normalized[name.strip()] = value
        if not normalized:
            raise RealPilotConfigurationError("estimated_max_costs must not be empty")
        object.__setattr__(self, "estimated_max_costs", normalized)
        object.__setattr__(self, "registry_path", Path(self.registry_path))
        if self.safe_default_model is not None and (
            not isinstance(self.safe_default_model, str)
            or not self.safe_default_model.strip()
        ):
            raise RealPilotConfigurationError("safe_default_model must be non-empty")


@dataclass
class _ExecutionCapture:
    """Private bridge state retained until the generic runner returns."""

    mode: str
    route_result: Optional[RouteResult] = None
    classifier_metrics: Optional[Mapping[str, Any]] = None
    provider_metrics: Optional[Dict[str, Mapping[str, Any]]] = None
    provider_response_text: Optional[Dict[str, str]] = None
    response_text: Optional[str] = None
    allowed_path_state: Optional[Dict[str, Dict[str, Any]]] = None
    evidence_error: Optional[str] = None


class _EvidenceProvider:
    """Delegate provider behavior while retaining only normalized response text."""

    def __init__(self, provider: Any, model_name: str, capture: _ExecutionCapture) -> None:
        self._provider = provider
        self._model_name = model_name
        self._capture = capture

    @property
    def last_call_metrics(self) -> Any:
        return getattr(self._provider, "last_call_metrics", None)

    def invoke(self, request: Any) -> Any:
        response = self._provider.invoke(request)
        if isinstance(response, ModelResponse) and isinstance(response.text, str):
            if len(response.text.encode("utf-8")) <= MAX_EVIDENCE_RESPONSE_BYTES:
                if self._capture.provider_response_text is None:
                    self._capture.provider_response_text = {}
                self._capture.provider_response_text[self._model_name] = response.text
            else:
                self._capture.evidence_error = "response_evidence_too_large"
        return response


def build_real_router(
    runtime: RealPilotRuntimeConfig,
    *,
    jev_factory: Optional[Callable[[], Any]] = None,
    provider_factory: Optional[Callable[[str, ModelDefinition], Any]] = None,
    _evidence_capture: Optional[_ExecutionCapture] = None,
) -> Tuple[Router, Any, Mapping[str, Any]]:
    """Build the real Pilot Router from the dedicated non-secret registry.

    ``jev_factory`` and ``provider_factory`` are test seams.  Production use
    leaves them unset, which constructs ``RealJEVClassifier`` from
    ``JEV_API_KEY_FILE`` and one ``OpenCodeGoProvider`` for every enabled
    registry model.  No credential value is accepted by this interface.
    """

    registry = ModelRegistry.from_yaml(runtime.registry_path)
    enabled_names = registry.enabled_names()
    if not enabled_names:
        raise RealPilotConfigurationError("real-pilot registry has no enabled models")
    missing_estimates = [
        name for name in enabled_names if name not in runtime.estimated_max_costs
    ]
    if missing_estimates:
        raise RealPilotConfigurationError(
            "missing max-cost estimates for enabled models"
        )
    if runtime.baseline_model not in enabled_names:
        raise RealPilotConfigurationError("baseline_model is not an enabled registry model")

    safe_default = runtime.safe_default_model or enabled_names[0]
    if safe_default not in enabled_names:
        raise RealPilotConfigurationError("safe_default_model is not an enabled registry model")

    providers: Dict[str, Any] = {}
    for name in enabled_names:
        definition = registry.get(name)
        if definition.provider != "opencode_go":
            raise RealPilotConfigurationError(
                "real-pilot registry contains a non-OpenCode provider"
            )
        provider = (
            provider_factory(name, definition)
            if provider_factory is not None
            else OpenCodeGoProvider(timeout=runtime.provider_timeout_seconds)
        )
        providers[name] = (
            _EvidenceProvider(provider, name, _evidence_capture)
            if _evidence_capture is not None
            else provider
        )

    jev = jev_factory() if jev_factory is not None else RealJEVClassifier()
    resolve_cost = getattr(jev, "resolve_cost", None)
    if not callable(resolve_cost):
        raise RealPilotConfigurationError("JEV classifier must expose resolve_cost")

    router = Router(
        registry,
        providers,
        jev,
        safe_default_model=safe_default,
        estimated_max_costs=runtime.estimated_max_costs,
        classifier_cost_resolver=resolve_cost,
    )
    return router, jev, providers


def run_real_pilot_pair(
    fixture_path: Union[os.PathLike, str],
    runtime: RealPilotRuntimeConfig,
    *,
    jev_factory: Optional[Callable[[], Any]] = None,
    provider_factory: Optional[Callable[[str, ModelDefinition], Any]] = None,
) -> Dict[str, Any]:
    """Run exactly one baseline/router pair on independent fixture copies.

    The returned pair is safe to print or pass to :func:`persist_pair`.  It
    contains no prompt, context, credential path/value, authorization header,
    raw provider output, or acceptance stdout/stderr.
    """

    spec = load_fixture(fixture_path)
    if spec.task_id == "task_010_fallback":
        raise ControlledMockFixtureError()

    baseline_capture = _ExecutionCapture(mode="baseline", provider_metrics={}, provider_response_text={})
    router_capture = _ExecutionCapture(mode="router", provider_metrics={}, provider_response_text={})
    baseline_router, baseline_jev, baseline_providers = build_real_router(
        runtime,
        jev_factory=_BaselineJEVSentinel,
        provider_factory=provider_factory,
        _evidence_capture=baseline_capture,
    )

    baseline_run = run_fixture(
        spec.path,
        "baseline",
        baseline_executor=_make_executor(
            baseline_router,
            runtime,
            "baseline",
            baseline_capture,
            baseline_jev,
            baseline_providers,
        ),
        post_run_hook=_make_post_run_hook(baseline_capture),
    )
    router_router, router_jev, router_providers = build_real_router(
        runtime,
        jev_factory=jev_factory,
        provider_factory=provider_factory,
        _evidence_capture=router_capture,
    )
    router_run = run_fixture(
        spec.path,
        "router",
        router_executor=_make_executor(
            router_router,
            runtime,
            "router",
            router_capture,
            router_jev,
            router_providers,
        ),
        post_run_hook=_make_post_run_hook(router_capture),
    )
    return {
        "official_pilot": False,
        "task_id": spec.task_id,
        "baseline": _safe_record(baseline_run, baseline_capture),
        "router": _safe_record(router_run, router_capture),
        "_evidence": {
            "baseline": _evidence_record(baseline_capture),
            "router": _evidence_record(router_capture),
        },
    }


def persist_pair(path: Union[os.PathLike, str], pair: Mapping[str, Any]) -> None:
    """Persist the two safe mode records, never to the official path by default."""

    target = Path(path)
    if _same_path(target, OFFICIAL_RESULT_PATH):
        raise RealPilotConfigurationError(
            "gate smoke must not write benchmark/results/pilot_runs.jsonl"
        )
    if pair.get("official_pilot") is not False:
        raise RealPilotConfigurationError("persisted gate records require official_pilot=false")
    for mode in ("baseline", "router"):
        record = pair.get(mode)
        if not isinstance(record, Mapping) or record.get("official_pilot") is not False:
            raise RealPilotConfigurationError("pair is missing a sanitized mode record")
        append_jsonl(target, record)


def append_jsonl(
    path: Union[os.PathLike, str],
    record: Mapping[str, Any],
    *,
    allow_official: bool = False,
    _official_token: Any = None,
) -> None:
    """Append one UTF-8 JSON object as one line, rejecting non-finite values."""

    target = Path(path)
    if allow_official and _official_token is not _OFFICIAL_APPEND_TOKEN:
        raise RealPilotConfigurationError(
            "official pilot result writes require the internal authorization path"
        )
    if _same_path(target, OFFICIAL_RESULT_PATH) and not allow_official:
        raise RealPilotConfigurationError(
            "official pilot result path requires explicit allow_official=True"
        )
    if not isinstance(record, Mapping):
        raise TypeError("record must be a mapping")
    _reject_non_finite(record)
    try:
        encoded = (
            json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RealPilotConfigurationError("record is not JSON serializable") from exc

    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    descriptor = os.open(str(target), flags, 0o600)
    try:
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _make_executor(
    router: Router,
    runtime: RealPilotRuntimeConfig,
    mode: str,
    capture: _ExecutionCapture,
    jev: Any,
    providers: Mapping[str, Any],
) -> Callable[[Path, Mapping[str, Any], str], Mapping[str, str]]:
    manual_model = runtime.baseline_model if mode == "baseline" else None

    def execute(
        workspace: Path,
        task_metadata: Mapping[str, Any],
        prompt: str,
    ) -> Mapping[str, str]:
        task_id = task_metadata.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise RealPilotConfigurationError("fixture metadata has no task id")
        request = RouteRequest(
            task_id=task_id,
            prompt=prompt,
            context=[],
            manual_model=manual_model,
            budget_limit=runtime.budget_limit,
            metadata={"cwd": str(workspace), "auto": True},
        )
        result = router.route(request)
        capture.route_result = result
        if result.selected_model and capture.provider_response_text:
            capture.response_text = capture.provider_response_text.get(result.selected_model)
        capture.classifier_metrics = _last_metrics(jev)
        capture.provider_metrics = {
            name: metrics
            for name, provider in providers.items()
            if (metrics := _last_metrics(provider)) is not None
        }
        return {"status": result.status}

    return execute


def _make_post_run_hook(
    capture: _ExecutionCapture,
) -> Callable[[Path, Any], None]:
    def capture_final_state(workspace: Path, spec: Any) -> None:
        try:
            capture.allowed_path_state = _capture_allowed_path_state(
                workspace, spec.allowed_paths
            )
        except (OSError, UnicodeError, ValueError, RealPilotConfigurationError) as exc:
            capture.evidence_error = str(exc).split(":", 1)[0]

    return capture_final_state


def _evidence_record(capture: _ExecutionCapture) -> Dict[str, Any]:
    return {
        "response_text": capture.response_text or "",
        "allowed_paths": capture.allowed_path_state or {},
        "error": capture.evidence_error,
    }


def _capture_allowed_path_state(
    workspace: Path,
    allowed_paths: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    root = workspace.resolve(strict=True)
    state: Dict[str, Dict[str, Any]] = {}
    total_bytes = 0
    for raw_allowed in allowed_paths:
        allowed = _validate_relative_evidence_path(raw_allowed)
        target = _safe_evidence_path(root, allowed)
        if not target.exists():
            state[allowed] = {"status": "absent"}
            continue
        _reject_symlink_components(root, target)
        if target.is_symlink():
            raise RealPilotConfigurationError("evidence symlink is not allowed")
        if target.is_file():
            item, size = _capture_text_file(target)
            state[allowed] = item
            total_bytes += size
        elif target.is_dir():
            found = False
            for current, directories, files in os.walk(target, followlinks=False):
                current_path = Path(current)
                for name in directories + files:
                    if (current_path / name).is_symlink():
                        raise RealPilotConfigurationError("evidence symlink is not allowed")
                for name in files:
                    found = True
                    item_path = current_path / name
                    relative = item_path.relative_to(root).as_posix()
                    item, size = _capture_text_file(item_path)
                    state[relative] = item
                    total_bytes += size
            if not found:
                state[allowed] = {"status": "present", "kind": "directory"}
        else:
            raise RealPilotConfigurationError("evidence path is not a regular file or directory")
        if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
            raise RealPilotConfigurationError("evidence_files_too_large")
    return state


def _capture_text_file(path: Path) -> Tuple[Dict[str, Any], int]:
    with path.open("rb") as stream:
        data = stream.read(MAX_EVIDENCE_FILE_BYTES + 1)
    if len(data) > MAX_EVIDENCE_FILE_BYTES:
        raise RealPilotConfigurationError("evidence_file_too_large")
    text = data.decode("utf-8")
    return {
        "status": "present",
        "kind": "file",
        "sha256": hashlib.sha256(data).hexdigest(),
        "text": text,
    }, len(data)


def _validate_relative_evidence_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RealPilotConfigurationError("evidence path is not a safe relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise RealPilotConfigurationError("evidence path is not a safe relative path")
    return pure.as_posix()


def _safe_evidence_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RealPilotConfigurationError("evidence path escapes workspace") from exc
    return candidate


def _reject_symlink_components(root: Path, path: Path) -> None:
    current = root
    for component in path.relative_to(root).parts:
        current = current / component
        if current.is_symlink():
            raise RealPilotConfigurationError("evidence symlink is not allowed")


def _safe_record(
    run_result: Mapping[str, Any], capture: _ExecutionCapture
) -> Dict[str, Any]:
    route_result = capture.route_result
    route_errors = [] if route_result is None else [error.code for error in route_result.errors]
    harness_errors = [
        error.get("code")
        for error in run_result.get("errors", [])
        if isinstance(error, Mapping) and isinstance(error.get("code"), str)
    ]
    error_codes = _unique_strings(route_errors + harness_errors)
    acceptance = run_result.get("acceptance")
    acceptance_status = (
        acceptance.get("status") if isinstance(acceptance, Mapping) else "not_run"
    )
    return {
        "official_pilot": False,
        "task_id": run_result.get("task_id"),
        "mode": run_result.get("mode"),
        "status": run_result.get("status"),
        "route_status": None if route_result is None else route_result.status,
        "route_source": None if route_result is None else route_result.route_source,
        "selected_model": None if route_result is None else route_result.selected_model,
        "classifier": _safe_classifier(
            None if route_result is None else route_result.classifier
        ),
        "classifier_metrics": _safe_metrics(capture.classifier_metrics, "jev"),
        "fallback_history": []
        if route_result is None
        else list(route_result.fallback_history),
        "cost": None
        if route_result is None
        else route_result.cost.model_dump(mode="json"),
        "error_codes": error_codes,
        "provider_metrics": {
            name: _safe_metrics(metrics, "provider")
            for name, metrics in (capture.provider_metrics or {}).items()
        },
        "acceptance_status": acceptance_status,
        "source_unchanged": bool(run_result.get("source_unchanged")),
        "workspace_removed": bool(run_result.get("workspace_removed")),
    }


def _safe_classifier(classifier: Any) -> Optional[Dict[str, Any]]:
    if classifier is None:
        return None
    return {
        "schema_version": classifier.schema_version,
        "task_type": classifier.task_type.value,
        "difficulty_score": classifier.difficulty_score,
        "difficulty_bucket": classifier.difficulty_bucket.value,
        "required_capability": classifier.required_capability.value,
        "confidence": classifier.confidence,
        "risk_level": classifier.risk_level.value,
    }


def _safe_metrics(metrics: Optional[Mapping[str, Any]], kind: str) -> Optional[Dict[str, Any]]:
    if metrics is None:
        return None
    allowed = (
        {
            "returned_model",
            "request_id",
            "input_tokens",
            "output_tokens",
            "latency_ms",
            "exact_cost",
        }
        if kind == "jev"
        else {
            "model_id",
            "request_id",
            "input_tokens",
            "output_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "provider_reported_cost",
            "latency_ms",
        }
    )
    result: Dict[str, Any] = {}
    for name in allowed:
        if name not in metrics:
            continue
        value = metrics[name]
        if value is None or isinstance(value, str):
            result[name] = value
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[name] = value
        elif isinstance(value, (float, Decimal)):
            _validate_finite_non_negative(value, f"{kind}.{name}")
            result[name] = float(value)
    return result


def _last_metrics(value: Any) -> Optional[Mapping[str, Any]]:
    metrics = getattr(value, "last_call_metrics", None)
    return metrics if isinstance(metrics, Mapping) else None


def _validate_finite_non_negative(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise RealPilotConfigurationError(f"{label} must be finite and non-negative")
    if not math.isfinite(float(value)) or value < 0:
        raise RealPilotConfigurationError(f"{label} must be finite and non-negative")


def _validate_positive_finite(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise RealPilotConfigurationError(f"{label} must be finite and positive")
    if not math.isfinite(float(value)) or value <= 0:
        raise RealPilotConfigurationError(f"{label} must be finite and positive")


def _reject_non_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise RealPilotConfigurationError("record contains a non-finite number")
    if isinstance(value, Decimal) and not value.is_finite():
        raise RealPilotConfigurationError("record contains a non-finite number")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_finite(item)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.expanduser().resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def _unique_strings(values: Sequence[Any]) -> list:
    result = []
    for value in values:
        if isinstance(value, str) and value not in result:
            result.append(value)
    return result
