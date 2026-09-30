"""Fail-closed, one-task-at-a-time Router-only Adjust execution gate.

This module is deliberately separate from the historical Pilot writer.  It
does not update workflow state, does not invoke Baseline, and never writes to
the original Pilot result or artifact paths.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from benchmark.pilot_adjust import _validate_completed_source, build_adjust_runtime_config
from benchmark.pilot_adjust_config import DEFAULT_CONFIG_PATH, load_pilot_adjust_config
from benchmark.real_pilot import (
    DEFAULT_REGISTRY_PATH,
    MAX_EVIDENCE_FILE_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    MAX_EVIDENCE_TOTAL_BYTES,
    _ExecutionCapture,
    _evidence_record,
    _make_executor,
    _make_post_run_hook,
    _safe_record,
    build_real_router,
)
from benchmark.runner import load_fixture, run_fixture
from jev_router.registry import ModelRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW_STATE_PATH = PROJECT_ROOT / "orchestration" / "workflow_state.json"
DEFAULT_FIXTURE_ROOT = PROJECT_ROOT / "benchmark" / "fixtures"
ADJUST_CONFIRMATION_TEXT = "OFFICIAL_ADJUST_ONE_TASK"
ADJUST_MANUAL_REVIEW_CONFIRMATION_TEXT = "OFFICIAL_ADJUST_MANUAL_REVIEW"
ADJUST_JEV_REVIEW_CONFIRMATION_TEXT = "OFFICIAL_ADJUST_JEV_REVIEW"
ADJUST_MODE = "router_adjusted"
ADJUST_PHASE = "timeout_fallback_budget_only"
_AUTH_TOKEN = object()


class PilotAdjustExecutionError(ValueError):
    code = "pilot_adjust_execution_error"

    def __init__(self, detail: str, code: Optional[str] = None):
        self.error_code = code or self.code
        super().__init__(f"{self.error_code}: {detail}")


@dataclass(frozen=True)
class AdjustPreflight:
    task_id: str
    project_root: Path
    config_path: Path
    workflow_state_path: Path
    fixture_path: Path
    registry_path: Path
    results_path: Path
    artifacts_dir: Path
    original_baseline: Mapping[str, Any]
    original_router: Mapping[str, Any]
    source_bytes: Mapping[str, bytes]
    existing_records: Tuple[Mapping[str, Any], ...]


def issue_adjust_authorization() -> object:
    """Return an opaque authorization token for the CLI/tests, never serializable."""

    return _AUTH_TOKEN


def preflight_adjust_task(
    task_id: str,
    *,
    config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    project_root: Union[str, Path] = PROJECT_ROOT,
    workflow_state_path: Union[str, Path] = DEFAULT_WORKFLOW_STATE_PATH,
    fixture_root: Union[str, Path] = DEFAULT_FIXTURE_ROOT,
    registry_path: Union[str, Path] = DEFAULT_REGISTRY_PATH,
) -> Dict[str, Any]:
    """Perform pure validation for exactly the next frozen Adjust task."""

    preflight = _build_preflight(
        task_id,
        config_path=config_path,
        project_root=project_root,
        workflow_state_path=workflow_state_path,
        fixture_root=fixture_root,
        registry_path=registry_path,
    )
    return _preflight_summary(preflight)


def execute_adjust_task(
    task_id: str,
    *,
    authorization: object,
    config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    project_root: Union[str, Path] = PROJECT_ROOT,
    workflow_state_path: Union[str, Path] = DEFAULT_WORKFLOW_STATE_PATH,
    fixture_root: Union[str, Path] = DEFAULT_FIXTURE_ROOT,
    registry_path: Union[str, Path] = DEFAULT_REGISTRY_PATH,
    jev_factory: Optional[Callable[[], Any]] = None,
    provider_factory: Optional[Callable[[str, Any], Any]] = None,
    timestamp_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one adjusted Router attempt after explicit authorization only."""

    _require_auth(authorization, ADJUST_CONFIRMATION_TEXT)
    preflight = _build_preflight(
        task_id,
        config_path=config_path,
        project_root=project_root,
        workflow_state_path=workflow_state_path,
        fixture_root=fixture_root,
        registry_path=registry_path,
    )
    if jev_factory is None:
        _require_key_file()

    config = load_pilot_adjust_config(config_path)
    runtime = replace(
        build_adjust_runtime_config(config),
        registry_path=preflight.registry_path,
    )
    capture = _ExecutionCapture(mode="router", provider_metrics={}, provider_response_text={})
    router, jev, providers = build_real_router(
        runtime,
        jev_factory=jev_factory,
        provider_factory=provider_factory,
        _evidence_capture=capture,
    )
    run_result = run_fixture(
        preflight.fixture_path,
        "router",
        router_executor=_make_executor(router, runtime, "router", capture, jev, providers),
        post_run_hook=_make_post_run_hook(capture),
    )
    _assert_sources_unchanged(preflight)
    safe = _safe_record(run_result, capture)
    record = _build_record(preflight, config, runtime, safe, capture, timestamp_utc)
    artifact = _write_artifact(preflight, capture, record)
    record["artifact"] = {key: value for key, value in artifact.items() if key != "absolute_path"}
    try:
        _append_adjust_record(preflight.results_path, record)
    except Exception as exc:
        _remove_path(artifact["absolute_path"])
        raise PilotAdjustExecutionError("Adjust JSONL write failed; staged artifact removed", "atomic_jsonl_write_failed") from exc
    record["artifact"] = {key: value for key, value in artifact.items() if key != "absolute_path"}
    return record


def finalize_adjust_manual_acceptance(
    task_id: str,
    passed: bool,
    *,
    authorization: object,
    config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    project_root: Union[str, Path] = PROJECT_ROOT,
    workflow_state_path: Union[str, Path] = DEFAULT_WORKFLOW_STATE_PATH,
    fixture_root: Union[str, Path] = DEFAULT_FIXTURE_ROOT,
    registry_path: Union[str, Path] = DEFAULT_REGISTRY_PATH,
    timestamp_utc: Optional[str] = None,
) -> Dict[str, Any]:
    _require_auth(authorization, ADJUST_MANUAL_REVIEW_CONFIRMATION_TEXT)
    if not isinstance(passed, bool):
        raise PilotAdjustExecutionError("manual result must be pass or fail")
    preflight = _build_preflight(
        task_id, config_path=config_path, project_root=project_root,
        workflow_state_path=workflow_state_path, fixture_root=fixture_root,
        registry_path=registry_path, allow_completed_current=True,
    )
    records = list(preflight.existing_records)
    record = _record_for(records, task_id)
    if record.get("acceptance_status") != "pending_manual":
        raise PilotAdjustExecutionError("manual acceptance is not pending")
    _verify_artifact(preflight, record)
    record["acceptance_status"] = "manual_passed" if passed else "manual_failed"
    record["manual_review"] = {
        "status": "passed" if passed else "failed",
        "evidence_integrity_verified": True,
        "timestamp_utc": timestamp_utc or _now(),
    }
    _rewrite_records(preflight.results_path, records)
    return {"status": "updated", "task_id": task_id, "acceptance_status": record["acceptance_status"]}


def finalize_adjust_jev_audit(
    task_id: str,
    assessment: str,
    *,
    authorization: object,
    config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    project_root: Union[str, Path] = PROJECT_ROOT,
    workflow_state_path: Union[str, Path] = DEFAULT_WORKFLOW_STATE_PATH,
    fixture_root: Union[str, Path] = DEFAULT_FIXTURE_ROOT,
    registry_path: Union[str, Path] = DEFAULT_REGISTRY_PATH,
    timestamp_utc: Optional[str] = None,
) -> Dict[str, Any]:
    _require_auth(authorization, ADJUST_JEV_REVIEW_CONFIRMATION_TEXT)
    if assessment not in {"reasonable", "questionable", "clearly_unreasonable"}:
        raise PilotAdjustExecutionError("invalid JEV audit assessment")
    preflight = _build_preflight(
        task_id, config_path=config_path, project_root=project_root,
        workflow_state_path=workflow_state_path, fixture_root=fixture_root,
        registry_path=registry_path, allow_completed_current=True,
    )
    records = list(preflight.existing_records)
    record = _record_for(records, task_id)
    audit = record.get("jev_audit")
    if not isinstance(audit, dict) or audit.get("status") != "pending_human_review":
        raise PilotAdjustExecutionError("JEV audit is not pending")
    _verify_artifact(preflight, record)
    audit["status"] = "reviewed"
    audit["assessment"] = assessment
    audit["timestamp_utc"] = timestamp_utc or _now()
    _rewrite_records(preflight.results_path, records)
    return {"status": "updated", "task_id": task_id, "jev_assessment": assessment}


def _build_preflight(task_id: str, *, config_path, project_root, workflow_state_path,
                     fixture_root, registry_path, allow_completed_current=False) -> AdjustPreflight:
    config = load_pilot_adjust_config(config_path)
    if task_id not in config.affected_task_ids:
        raise PilotAdjustExecutionError("task is not in the frozen Adjust task set", "task_not_affected")
    root = Path(project_root).expanduser().resolve(strict=True)
    state_path = Path(workflow_state_path).expanduser().resolve(strict=True)
    state = _read_json(state_path, "workflow state")
    _validate_workflow(state)
    _validate_paths(config, root)
    try:
        _validate_completed_source(config, root)
    except Exception as exc:
        raise PilotAdjustExecutionError("original Pilot evidence is incomplete", "source_incomplete") from exc
    registry = Path(registry_path).expanduser().resolve(strict=True)
    try:
        ModelRegistry.from_yaml(registry)
    except Exception as exc:
        raise PilotAdjustExecutionError("real-pilot registry is not loadable", "registry_invalid") from exc
    source_bytes = _load_original_sources(config, root)
    original = _load_original_records(root / config.source.results_path)
    original_baseline, original_router = _original_pair(original, task_id)
    outputs = root / config.outputs.adjust_results_path
    artifacts = root / config.outputs.adjust_artifacts_dir
    records = _load_adjust_records(outputs)
    _validate_artifact_orphans(artifacts, records)
    _validate_adjust_order(config.affected_task_ids, records, task_id, allow_completed_current)
    fixture = Path(fixture_root).expanduser().resolve(strict=True) / task_id
    try:
        spec = load_fixture(fixture)
    except Exception as exc:
        raise PilotAdjustExecutionError("affected fixture is not loadable", "fixture_invalid") from exc
    if spec.task_id != task_id:
        raise PilotAdjustExecutionError("fixture task id mismatch")
    return AdjustPreflight(
        task_id=task_id, project_root=root, config_path=Path(config_path).resolve(),
        workflow_state_path=state_path, fixture_path=fixture, registry_path=registry,
        results_path=outputs, artifacts_dir=artifacts,
        original_baseline=original_baseline, original_router=original_router,
        source_bytes=source_bytes, existing_records=tuple(records),
    )


def _preflight_summary(preflight: AdjustPreflight) -> Dict[str, Any]:
    config = load_pilot_adjust_config(preflight.config_path)
    return {
        "status": "preflight_ready", "task_id": preflight.task_id,
        "execution": "preflight_only", "writes_performed": False,
        "requires_jev_api_key": True,
        "router_only": True, "next_task_in_order": preflight.task_id,
        "adjust_results_path": _relative(preflight.results_path, preflight.project_root),
        "adjust_artifacts_dir": _relative(preflight.artifacts_dir, preflight.project_root),
        "frozen_parameters": {
            "provider_timeout_seconds": config.provider_timeout_seconds,
            "budget_limit_per_adjust_attempt": config.budget_limit_per_adjust_attempt,
            "estimated_max_costs": dict(config.estimated_max_costs),
            "affected_task_ids": list(config.affected_task_ids),
        },
    }


def _validate_workflow(state: Mapping[str, Any]) -> None:
    if state.get("status") != "pilot_adjust_phase1_passed" or state.get("current_task") != "PILOT_ADJUST_EXECUTION_GATE":
        raise PilotAdjustExecutionError("workflow is not at the Adjust execution gate", "workflow_gate_mismatch")
    prep = state.get("pilot_preparation")
    if not isinstance(prep, Mapping) or prep.get("current_task") != "PILOT_ADJUST_EXECUTION_GATE" or prep.get("execution_gate") != "adjust_execution_wiring_required":
        raise PilotAdjustExecutionError("pilot preparation gate is not ready", "workflow_gate_mismatch")


def _validate_paths(config, root: Path) -> None:
    original = [config.source.official_config_path, config.source.results_path,
                config.source.summary_json_path, config.source.summary_markdown_path,
                "benchmark/results/artifacts"]
    outputs = [config.outputs.adjust_results_path, config.outputs.adjust_artifacts_dir,
               config.outputs.adjust_summary_json_path, config.outputs.adjust_summary_markdown_path]
    original_paths = [(root / item).resolve(strict=False) for item in original]
    for output in outputs:
        target = (root / output).resolve(strict=False)
        if any(target == item or item in target.parents or target in item.parents for item in original_paths):
            raise PilotAdjustExecutionError("Adjust output collides with original Pilot evidence", "output_path_collision")


def _load_original_sources(config, root: Path) -> Dict[str, bytes]:
    result = {}
    for relative in (config.source.official_config_path, config.source.results_path,
                     config.source.summary_json_path, config.source.summary_markdown_path):
        path = root / relative
        if not path.is_file():
            raise PilotAdjustExecutionError("original Pilot evidence is incomplete", "source_incomplete")
        result[relative] = path.read_bytes()
    try:
        summary = json.loads(result[config.source.summary_json_path].decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise PilotAdjustExecutionError("original Pilot summary is unreadable", "source_invalid") from exc
    if not isinstance(summary, Mapping) or summary.get("decision") != "adjust" or summary.get("workflow_gate") != "pilot_summary_required":
        raise PilotAdjustExecutionError("original Pilot decision/evidence is not complete", "source_decision_invalid")
    return result


def _load_original_records(path: Path) -> Sequence[Mapping[str, Any]]:
    rows = _read_jsonl(path, "original Pilot results")
    if not rows:
        raise PilotAdjustExecutionError("original Pilot results are empty", "source_incomplete")
    for row in rows:
        if row.get("official_pilot") is not True:
            raise PilotAdjustExecutionError("original Pilot result is not official", "source_invalid")
    return rows


def _original_pair(rows, task_id):
    selected = {row.get("mode"): row for row in rows if row.get("task_id") == task_id and row.get("attempt", 1) == 1}
    if not {"baseline", "router"}.issubset(selected):
        raise PilotAdjustExecutionError("original first-attempt Baseline/Router pair is missing", "source_pair_missing")
    return selected["baseline"], selected["router"]


def _load_adjust_records(path: Path) -> list:
    if not path.exists():
        return []
    rows = _read_jsonl(path, "Adjust results")
    seen = set()
    for row in rows:
        _reject_unsafe_record(row)
        if row.get("mode") != ADJUST_MODE or row.get("attempt") != 1 or row.get("adjust_phase") != ADJUST_PHASE:
            raise PilotAdjustExecutionError("Adjust record contract is invalid", "adjust_record_invalid")
        task = row.get("task_id")
        if task in seen:
            raise PilotAdjustExecutionError("duplicate Adjust record", "adjust_duplicate")
        seen.add(task)
    return rows


def _reject_unsafe_record(value: Any) -> None:
    forbidden_names = {"prompt", "context", "stdout", "stderr", "raw_response", "raw_output", "authorization", "api_key"}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in forbidden_names:
                raise PilotAdjustExecutionError("Adjust record contains forbidden raw data", "adjust_record_unsafe")
            _reject_unsafe_record(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_unsafe_record(item)
    elif isinstance(value, str) and re.search(r"(?i)bearer\s+|api[_-]?key\s*[:=]", value):
        raise PilotAdjustExecutionError("Adjust record contains secret-like text", "adjust_record_unsafe")


def _validate_adjust_order(affected, rows, requested, allow_completed_current):
    ids = [row.get("task_id") for row in rows]
    if ids != list(affected[:len(ids)]):
        raise PilotAdjustExecutionError("Adjust results are out of order", "adjust_order_invalid")
    next_task = affected[len(ids)] if len(ids) < len(affected) else None
    if requested != next_task:
        if allow_completed_current and requested in ids:
            return
        raise PilotAdjustExecutionError("task is not the next unresolved Adjust task", "adjust_order_blocked")
    for row in rows:
        if row.get("acceptance_status") in {"pending_manual", "not_run"}:
            raise PilotAdjustExecutionError("previous Adjust manual acceptance is pending", "manual_review_pending")
        audit = row.get("jev_audit")
        if isinstance(audit, Mapping) and audit.get("status") == "pending_human_review":
            raise PilotAdjustExecutionError("previous JEV audit is pending", "jev_review_pending")


def _validate_artifact_orphans(artifacts: Path, records: Sequence[Mapping[str, Any]]) -> None:
    if not artifacts.exists():
        return
    known = {str(row.get("task_id")) for row in records}
    for task_dir in artifacts.iterdir():
        if task_dir.name.startswith("."):
            raise PilotAdjustExecutionError("staging artifact remains", "artifact_orphan")
        if task_dir.is_dir() and task_dir.name not in known:
            raise PilotAdjustExecutionError("orphan Adjust artifact blocks execution", "artifact_orphan")


def _build_record(preflight, config, runtime, safe, capture, timestamp):
    original_router = preflight.original_router
    original_baseline = preflight.original_baseline
    classifier = safe.get("classifier")
    return {
        "official_pilot": False, "mode": ADJUST_MODE, "adjust_phase": ADJUST_PHASE,
        "task_id": preflight.task_id, "attempt": 1, "source_pilot_attempt": 1,
        "timestamp_utc": timestamp or _now(), "router_only": True,
        **{key: safe.get(key) for key in (
            "status", "route_status", "route_source", "selected_model", "classifier",
            "classifier_metrics", "fallback_history", "error_codes", "cost",
            "provider_metrics", "acceptance_status", "source_unchanged", "workspace_removed",
        )},
        "adjusted_runtime": {
            "provider_timeout_seconds": float(runtime.provider_timeout_seconds),
            "budget_limit": float(runtime.budget_limit),
            "estimated_max_costs": {key: float(value) for key, value in runtime.estimated_max_costs.items()},
        },
        "original_router_route_status": original_router.get("route_status"),
        "original_router_acceptance": original_router.get("acceptance_status"),
        "original_router_selected_model": original_router.get("selected_model"),
        "original_router_cost": original_router.get("cost"),
        "original_baseline_acceptance": original_baseline.get("acceptance_status"),
        "original_baseline_cost": original_baseline.get("cost"),
        "jev_audit": ({
            "status": "pending_human_review", "assessment": None,
            "classifier_cost": _classifier_cost(safe.get("classifier_metrics")),
        } if classifier is not None else {"status": "not_applicable"}),
    }


def _classifier_cost(metrics):
    return metrics.get("exact_cost") if isinstance(metrics, Mapping) else None


def _write_artifact(preflight, capture, record):
    final = preflight.artifacts_dir / preflight.task_id / "attempt_1" / "router"
    if final.exists():
        raise PilotAdjustExecutionError("final artifact already exists", "artifact_duplicate")
    preflight.artifacts_dir.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".adjust-stage-", dir=str(preflight.artifacts_dir)))
    try:
        router_dir = stage
        evidence = _evidence_record(capture)
        response = _sanitize(str(evidence.get("response_text") or ""))
        _bounded(response.encode("utf-8"), MAX_EVIDENCE_RESPONSE_BYTES, "response")
        (router_dir / "response.txt").write_text(response, encoding="utf-8")
        files = [{"path": "response.txt", "sha256": _sha256((router_dir / "response.txt").read_bytes()), "size": len(response.encode("utf-8"))}]
        allowed = evidence.get("allowed_paths") or {}
        total = len(response.encode("utf-8"))
        if not isinstance(allowed, Mapping):
            raise PilotAdjustExecutionError("allowed evidence is malformed", "artifact_invalid")
        for relative, item in sorted(allowed.items()):
            if not isinstance(item, Mapping) or item.get("kind") != "file":
                continue
            text = _sanitize(str(item.get("text") or ""))
            data = text.encode("utf-8")
            _bounded(data, MAX_EVIDENCE_FILE_BYTES, "evidence file")
            total += len(data)
            if total > MAX_EVIDENCE_TOTAL_BYTES:
                raise PilotAdjustExecutionError("evidence bundle is too large", "artifact_too_large")
            safe_rel = _safe_relative(relative)
            target = router_dir / "files" / safe_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files.append({"path": f"files/{safe_rel}", "source_path": relative, "sha256": _sha256(data), "size": len(data)})
        manifest = {"schema_version": "1", "task_id": preflight.task_id, "attempt": 1, "mode": ADJUST_MODE, "files": files, "record_sha256": _sha256(_json_bytes(record))}
        manifest_path = router_dir / "manifest.json"
        manifest_bytes = _json_bytes(manifest)
        manifest_path.write_bytes(manifest_bytes)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(stage), str(final))
        _fsync_dir(final.parent)
        return {"path": _relative(final, preflight.project_root), "manifest_sha256": _sha256(manifest_bytes), "response_sha256": files[0]["sha256"], "absolute_path": str(final)}
    except PilotAdjustExecutionError:
        _remove_path(stage)
        raise
    except Exception as exc:
        _remove_path(stage)
        raise PilotAdjustExecutionError("artifact staging failed", "artifact_write_failed") from exc


def _verify_artifact(preflight, record):
    artifact = record.get("artifact")
    if not isinstance(artifact, Mapping):
        raise PilotAdjustExecutionError("artifact reference is missing", "artifact_invalid")
    path = preflight.project_root / str(artifact.get("path", ""))
    expected = preflight.artifacts_dir / preflight.task_id / "attempt_1" / "router"
    if path.resolve(strict=False) != expected.resolve(strict=False) or not path.is_dir():
        raise PilotAdjustExecutionError("artifact path is unsafe", "artifact_invalid")
    for current, directories, files in os.walk(path, followlinks=False):
        if any((Path(current) / name).is_symlink() for name in directories + files):
            raise PilotAdjustExecutionError("artifact symlink is not allowed", "artifact_invalid")
    manifest = path / "manifest.json"
    response = path / "response.txt"
    if not manifest.is_file() or not response.is_file():
        raise PilotAdjustExecutionError("artifact files are incomplete", "artifact_invalid")
    if _sha256(manifest.read_bytes()) != artifact.get("manifest_sha256") or _sha256(response.read_bytes()) != artifact.get("response_sha256"):
        raise PilotAdjustExecutionError("artifact hash verification failed", "artifact_tampered")
    return True


def _assert_sources_unchanged(preflight: AdjustPreflight) -> None:
    for relative, original in preflight.source_bytes.items():
        path = preflight.project_root / relative
        try:
            current = path.read_bytes()
        except OSError as exc:
            raise PilotAdjustExecutionError("original Pilot evidence disappeared", "source_changed") from exc
        if current != original:
            raise PilotAdjustExecutionError("original Pilot evidence changed", "source_changed")


def _append_adjust_record(path: Path, record: Mapping[str, Any]) -> None:
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        raise PilotAdjustExecutionError("Adjust JSONL is truncated", "adjust_jsonl_invalid")
    encoded = _json_bytes(record)
    if not encoded.endswith(b"\n"):
        encoded += b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, existing + encoded)


def _rewrite_records(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    _atomic_write(path, b"".join(_json_bytes(row) for row in records))


def _read_jsonl(path: Path, label: str) -> list:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PilotAdjustExecutionError(f"{label} is unreadable", "jsonl_unreadable") from exc
    if data and not data.endswith(b"\n"):
        raise PilotAdjustExecutionError(f"{label} is truncated", "jsonl_invalid")
    rows = []
    for line in data.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise PilotAdjustExecutionError(f"{label} is malformed", "jsonl_invalid") from exc
        if not isinstance(value, dict):
            raise PilotAdjustExecutionError(f"{label} contains a non-object", "jsonl_invalid")
        rows.append(value)
    return rows


def _record_for(records, task_id):
    found = [row for row in records if row.get("task_id") == task_id]
    if len(found) != 1:
        raise PilotAdjustExecutionError("Adjust record is missing or duplicated", "adjust_record_invalid")
    return found[0]


def _require_auth(value: object, expected: str) -> None:
    if value is not _AUTH_TOKEN:
        raise PilotAdjustExecutionError(f"confirmation required: {expected}", "authorization_required")


def _require_key_file() -> None:
    value = os.environ.get("JEV_API_KEY_FILE")
    if not value or not Path(value).is_file() or not os.access(value, os.R_OK):
        raise PilotAdjustExecutionError("JEV_API_KEY_FILE is required only for execution", "jev_api_key_required")


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    except Exception:
        _remove_path(Path(temporary))
        raise


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _remove_path(path: Union[str, Path]) -> None:
    target = Path(path)
    if target.is_dir() and not target.is_symlink():
        for child in list(target.iterdir()):
            _remove_path(child)
        target.rmdir()
    elif target.exists() or target.is_symlink():
        target.unlink()


def _safe_relative(value: str) -> str:
    value = value.replace("\\", "/")
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise PilotAdjustExecutionError("unsafe evidence path", "artifact_invalid")
    return "/".join(part for part in value.split("/") if part not in {"", "."})


def _sanitize(value: str) -> str:
    value = re.sub(r"(?i)bearer\s+[^\s,;]+", "[REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key|authorization|password|secret|credential)[^=:\n]*[=:]\s*[^\s,;]+", r"\1=[REDACTED]", value)
    value = re.sub(r"(?i)/(?:Users|home)/[^\s\n]+", "[REDACTED_PATH]", value)
    return value


def _bounded(data: bytes, limit: int, label: str) -> None:
    if len(data) > limit:
        raise PilotAdjustExecutionError(f"{label} evidence is too large", "artifact_too_large")


def _json_bytes(value: Any) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PilotAdjustExecutionError("unsafe JSON record", "json_invalid") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative(path: Path, root: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PilotAdjustExecutionError(f"{label} is invalid", "workflow_invalid") from exc
    if not isinstance(value, Mapping):
        raise PilotAdjustExecutionError(f"{label} is not an object", "workflow_invalid")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ADJUST_CONFIRMATION_TEXT", "ADJUST_JEV_REVIEW_CONFIRMATION_TEXT",
    "ADJUST_MANUAL_REVIEW_CONFIRMATION_TEXT", "PilotAdjustExecutionError",
    "execute_adjust_task", "finalize_adjust_jev_audit",
    "finalize_adjust_manual_acceptance", "issue_adjust_authorization",
    "preflight_adjust_task",
]
