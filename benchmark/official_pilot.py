"""Fail-closed, one-task-at-a-time official Pilot execution gate.

This module is the only supported path for writing official Pilot records.  It
does not change orchestration state and never advances to another task.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from benchmark.failure_injection import FailureInjectionCase, run_failure_injection
from benchmark.pilot_config import (
    CONTROLLED_FAILURE_TASK_ID,
    DEFAULT_CONFIG_PATH,
    DEFAULT_MODEL_REGISTRY_PATH,
    EXPECTED_REAL_TASK_IDS,
    PilotConfig,
    load_pilot_config,
)
from benchmark.real_pilot import (
    MAX_EVIDENCE_FILE_BYTES,
    MAX_EVIDENCE_RESPONSE_BYTES,
    MAX_EVIDENCE_TOTAL_BYTES,
    RealPilotRuntimeConfig,
    _OFFICIAL_APPEND_TOKEN,
    _last_metrics,
    _capture_allowed_path_state,
    _reject_non_finite,
    _safe_classifier,
    _safe_metrics,
    run_real_pilot_pair,
)
from benchmark.runner import FixtureSpec, _tree_hash, load_fixture
from jev_router.real_jev import RealJEVClassifier
from jev_router.registry import ModelRegistry
from jev_router.schemas import RouteRequest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW_STATE_PATH = PROJECT_ROOT / "orchestration" / "workflow_state.json"
DEFAULT_FIXTURE_ROOT = PROJECT_ROOT / "benchmark" / "fixtures"
CONFIRMATION_TEXT = "OFFICIAL_PILOT_ONE_TASK"
MANUAL_REVIEW_CONFIRMATION_TEXT = "OFFICIAL_PILOT_MANUAL_REVIEW"
_AUTH_TOKEN = object()
_PREFLIGHT_TOKEN = object()
_FORBIDDEN_KEYS = frozenset(
    {
        "prompt",
        "context",
        "raw_stdout",
        "raw_stderr",
        "raw_provider_response",
        "authorization",
        "api_key",
        "key_path",
        "key_value",
    }
)
_SECRET_TEXT = re.compile(r"(?i)(?:bearer\s+|api[_-]?key|authorization|/users/|/home/|sk-)")


class OfficialPilotGateError(ValueError):
    """Stable, sanitized failure from the official execution gate."""

    code = "official_pilot_gate_error"

    def __init__(self, detail: str, *, error_code: Optional[str] = None) -> None:
        self.error_code = error_code or detail
        super().__init__(f"{self.code}: {self.error_code}: {detail}")


@dataclass(frozen=True)
class _OfficialExecutionAuthorization:
    """Opaque authorization capability; callers cannot mint a valid token."""

    _auth_token: object
    _append_token: object


@dataclass(frozen=True)
class OfficialPreflight:
    """Side-effect-free proof that one exact task may be executed."""

    task_id: str
    fixture_path: Path
    source_fixture_hash: str
    results_path: Path
    next_task_id: str
    requires_jev_api_key: bool
    config_schema_version: str
    config: PilotConfig
    config_path: Path
    workflow_state_path: Path
    registry_path: Path
    fixture_root: Path
    artifacts_dir: Path
    acceptance_mode: str
    _proof: object

    def sanitized_summary(self) -> Dict[str, Any]:
        return {
            "official_pilot": True,
            "status": "preflight_passed",
            "task_id": self.task_id,
            "next_task_id": self.next_task_id,
            "requires_jev_api_key": self.requires_jev_api_key,
            "source_fixture_hash": self.source_fixture_hash,
            "config_schema_version": self.config_schema_version,
        }


def _issue_official_authorization() -> _OfficialExecutionAuthorization:
    """Create the capability used only after CLI confirmation."""

    return _OfficialExecutionAuthorization(_AUTH_TOKEN, _OFFICIAL_APPEND_TOKEN)


def preflight_official_task(
    task_id: str,
    *,
    config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    workflow_state_path: Union[str, Path] = DEFAULT_WORKFLOW_STATE_PATH,
    registry_path: Union[str, Path] = DEFAULT_MODEL_REGISTRY_PATH,
    fixture_root: Union[str, Path] = DEFAULT_FIXTURE_ROOT,
    results_path: Optional[Union[str, Path]] = None,
) -> OfficialPreflight:
    """Validate one task without network, providers, credentials, or writes."""

    _validate_task_id(task_id)
    try:
        config = load_pilot_config(config_path, model_registry_path=registry_path)
    except Exception as exc:
        raise OfficialPilotGateError("pilot config or registry is not loadable", error_code="pilot_config_unloadable") from exc

    workflow_phase = _validate_workflow_state(workflow_state_path)
    resolved_outputs = _validate_output_paths(config)
    official_results_path = resolved_outputs["official_results_path"]
    if results_path is not None:
        supplied = _resolve_path(results_path)
        if supplied != official_results_path:
            raise OfficialPilotGateError("results path does not match frozen config", error_code="results_path_mismatch")

    records = _read_official_records(official_results_path)
    _validate_phase_results(workflow_phase, records, config)
    if task_id == EXPECTED_REAL_TASK_IDS[0] and workflow_phase != "before_first_official_run":
        raise OfficialPilotGateError(
            "task_001 is only allowed in the pre-first-run workflow phase",
            error_code="workflow_phase_task_mismatch",
        )
    if task_id != EXPECTED_REAL_TASK_IDS[0] and workflow_phase != "official_pilot_running":
        raise OfficialPilotGateError(
            "task_002 through task_010 require the running workflow phase",
            error_code="workflow_phase_task_mismatch",
        )
    if task_id in EXPECTED_REAL_TASK_IDS and _artifact_task_root(resolved_outputs["artifacts_dir"], task_id).exists():
        raise OfficialPilotGateError(
            "official evidence artifact already exists",
            error_code="artifact_orphan_or_duplicate",
        )
    if _task_has_complete_pair(records, task_id):
        raise OfficialPilotGateError(
            "task already has a completed official first-attempt pair",
            error_code="duplicate_official_record",
        )
    next_task = _next_allowed_task(records, config)
    if task_id != next_task:
        raise OfficialPilotGateError(
            f"task ordering requires {next_task}",
            error_code="task_order_violation",
        )

    fixture_path = Path(fixture_root) / task_id
    try:
        spec = load_fixture(fixture_path)
        source_hash = _tree_hash(spec.path)
    except Exception as exc:
        raise OfficialPilotGateError("fixture is not loadable or hashable", error_code="fixture_unloadable") from exc
    if spec.task_id != task_id:
        raise OfficialPilotGateError("fixture task id does not match request", error_code="fixture_task_mismatch")

    requires_key = task_id in EXPECTED_REAL_TASK_IDS
    if requires_key:
        _require_jev_key_file()

    return OfficialPreflight(
        task_id=task_id,
        fixture_path=spec.path,
        source_fixture_hash=source_hash,
        results_path=official_results_path,
        next_task_id=next_task,
        requires_jev_api_key=requires_key,
        config_schema_version=config.schema_version,
        config=config,
        config_path=_resolve_path(config_path),
        workflow_state_path=_resolve_path(workflow_state_path),
        registry_path=_resolve_path(registry_path),
        fixture_root=_resolve_path(fixture_root),
        artifacts_dir=resolved_outputs["artifacts_dir"],
        acceptance_mode=spec.acceptance_mode,
        _proof=_PREFLIGHT_TOKEN,
    )


def execute_official_task(
    task_id: str,
    authorization: _OfficialExecutionAuthorization,
    preflight: OfficialPreflight,
    *,
    jev_factory: Optional[Callable[[], Any]] = None,
    provider_factory: Optional[Callable[..., Any]] = None,
    audit_jev_factory: Optional[Callable[[], Any]] = None,
    timestamp_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute exactly one preflighted task and append its complete pair."""

    _require_authorization(authorization)
    _require_preflight(task_id, preflight)
    _refresh_preflight(preflight)
    if task_id in EXPECTED_REAL_TASK_IDS:
        _require_jev_key_file()

    artifact_task_dir: Optional[Path] = None
    if task_id == CONTROLLED_FAILURE_TASK_ID:
        baseline, router = _run_controlled_task(preflight)
    else:
        runtime = RealPilotRuntimeConfig(
            baseline_model=preflight.config.baseline_model,
            budget_limit=preflight.config.budget_limit_per_real_task,
            estimated_max_costs=preflight.config.estimated_max_costs,
            registry_path=preflight.registry_path,
        )
        pair = run_real_pilot_pair(
            preflight.fixture_path,
            runtime,
            jev_factory=jev_factory,
            provider_factory=provider_factory,
        )
        _validate_bridge_pair(pair, task_id)
        evidence = pair.get("_evidence")
        if not isinstance(evidence, Mapping):
            raise OfficialPilotGateError(
                "real-pilot evidence capture is missing",
                error_code="evidence_missing",
            )
        baseline = _officialize_record(
            pair["baseline"],
            preflight,
            timestamp_utc=timestamp_utc,
            jev_audit=_baseline_audit(),
            experimental_validation_cost=None,
        )
        router_audit, experimental_cost = _router_audit(
            pair["router"],
            preflight,
            audit_jev_factory=audit_jev_factory,
        )
        router = _officialize_record(
            pair["router"],
            preflight,
            timestamp_utc=timestamp_utc,
            jev_audit=router_audit,
            experimental_validation_cost=experimental_cost,
        )

    _validate_official_pair(baseline, router, task_id)
    if task_id != CONTROLLED_FAILURE_TASK_ID:
        try:
            artifact_refs, artifact_task_dir = _materialize_evidence_bundle(
                preflight,
                evidence,
                {"baseline": baseline, "router": router},
            )
            baseline = _attach_artifact_ref(baseline, artifact_refs["baseline"])
            router = _attach_artifact_ref(router, artifact_refs["router"])
            _validate_official_pair(baseline, router, task_id)
            persist_official_pair(authorization, preflight, baseline, router)
        except Exception:
            if artifact_task_dir is not None and artifact_task_dir.exists():
                _remove_artifact_bundle(artifact_task_dir)
            raise
    else:
        persist_official_pair(authorization, preflight, baseline, router)
    return {
        "official_pilot": True,
        "status": "persisted",
        "task_id": task_id,
        "attempt": 1,
        "records_written": 2,
        "results_path": "benchmark/results/pilot_runs.jsonl",
    }


def persist_official_pair(
    authorization: _OfficialExecutionAuthorization,
    preflight: OfficialPreflight,
    baseline: Mapping[str, Any],
    router: Mapping[str, Any],
) -> None:
    """Write a previously validated pair through the sole official path."""

    _require_authorization(authorization)
    _require_preflight(preflight.task_id, preflight)
    _validate_official_pair(baseline, router, preflight.task_id)
    baseline_line = _serialize_official_record(baseline)
    router_line = _serialize_official_record(router)
    existing_bytes = _read_official_bytes(preflight.results_path)
    if existing_bytes:
        _decode_official_records(existing_bytes)
    _atomic_replace_official_jsonl(
        preflight.results_path,
        existing_bytes + baseline_line + router_line,
    )


def finalize_manual_acceptance(
    task_id: str,
    authorization: _OfficialExecutionAuthorization,
    baseline_passed: bool,
    router_passed: bool,
    *,
    config_path: Union[str, Path] = DEFAULT_CONFIG_PATH,
    fixture_root: Union[str, Path] = DEFAULT_FIXTURE_ROOT,
    results_path: Optional[Union[str, Path]] = None,
    timestamp_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """Record advisor verdicts for one already persisted manual pair."""

    _require_authorization(authorization)
    _validate_task_id(task_id)
    if not isinstance(baseline_passed, bool) or not isinstance(router_passed, bool):
        raise OfficialPilotGateError("manual verdicts must be boolean", error_code="manual_verdict_invalid")
    try:
        config = load_pilot_config(config_path, model_registry_path=DEFAULT_MODEL_REGISTRY_PATH)
    except Exception as exc:
        raise OfficialPilotGateError("pilot config is not loadable", error_code="pilot_config_unloadable") from exc
    outputs = _validate_output_paths(config)
    official_results_path = outputs["official_results_path"]
    if results_path is not None and _resolve_path(results_path) != official_results_path:
        raise OfficialPilotGateError("results path does not match frozen config", error_code="results_path_mismatch")
    records = list(_read_official_records(official_results_path))
    matches = [record for record in records if record.get("task_id") == task_id]
    if len(matches) != 2 or {record.get("mode") for record in matches} != {"baseline", "router"}:
        raise OfficialPilotGateError("manual review requires exactly one official pair", error_code="manual_pair_invalid")
    spec = load_fixture(Path(fixture_root) / task_id)
    if spec.acceptance_mode != "manual":
        raise OfficialPilotGateError("manual review is only valid for manual fixtures", error_code="manual_review_not_required")
    for record in matches:
        if record.get("attempt") != 1 or record.get("acceptance_status") != "pending_manual":
            raise OfficialPilotGateError("manual pair is not pending review", error_code="manual_acceptance_not_pending")
        _verify_artifact_reference(
            record,
            outputs["artifacts_dir"],
            task_id,
            spec.allowed_paths,
        )

    verdicts = {"baseline": baseline_passed, "router": router_passed}
    reviewed_at = timestamp_utc or _utc_now()
    updated = []
    for record in records:
        if record.get("task_id") == task_id:
            mode = record["mode"]
            value = dict(record)
            value["acceptance_status"] = "manual_passed" if verdicts[mode] else "manual_failed"
            value["manual_review"] = {
                "reviewer": "advisor",
                "evidence_verified": True,
                "timestamp_utc": reviewed_at,
            }
            updated.append(value)
        else:
            updated.append(dict(record))
    _atomic_replace_official_jsonl(
        official_results_path,
        b"".join(_serialize_official_record(record) for record in updated),
    )
    return {
        "official_pilot": True,
        "status": "manual_review_finalized",
        "task_id": task_id,
        "baseline_acceptance_status": "manual_passed" if baseline_passed else "manual_failed",
        "router_acceptance_status": "manual_passed" if router_passed else "manual_failed",
    }


def _verify_artifact_reference(
    record: Mapping[str, Any],
    artifacts_dir: Path,
    task_id: str,
    allowed_paths: Sequence[str],
) -> None:
    relative = _validate_artifact_relative(record.get("evidence_artifact"))
    candidate = (PROJECT_ROOT / relative).resolve(strict=False)
    expected = (artifacts_dir / task_id / "attempt_1" / record.get("mode", "")).resolve(strict=False)
    try:
        candidate.relative_to(artifacts_dir.resolve())
    except ValueError as exc:
        raise OfficialPilotGateError("manual artifact path is outside frozen artifacts root", error_code="manual_artifact_invalid") from exc
    if candidate != expected or not candidate.is_dir():
        raise OfficialPilotGateError("manual artifact reference is invalid", error_code="manual_artifact_invalid")
    manifest_path = candidate / "manifest.json"
    response_path = candidate / "response.txt"
    _require_artifact_file(manifest_path, candidate)
    _require_artifact_file(response_path, candidate)
    expected_hash = record.get("evidence_manifest_sha256")
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise _manual_artifact_content_mismatch("manifest is unreadable") from exc
    actual_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise OfficialPilotGateError("manual artifact manifest hash mismatch", error_code="manual_artifact_hash_mismatch")
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _manual_artifact_content_mismatch("manifest is not valid JSON") from exc
    if not isinstance(manifest, Mapping):
        raise _manual_artifact_content_mismatch("manifest is not an object")
    if (
        manifest.get("schema_version") != "0.1"
        or manifest.get("task_id") != task_id
        or manifest.get("attempt") != 1
        or manifest.get("mode") != record.get("mode")
    ):
        raise _manual_artifact_content_mismatch("manifest identity is invalid")
    response_sha256 = manifest.get("response_sha256")
    if not _is_sha256(response_sha256):
        raise _manual_artifact_content_mismatch("response hash is invalid")
    try:
        response_bytes = response_path.read_bytes()
        response_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _manual_artifact_content_mismatch("response is unreadable") from exc
    if hashlib.sha256(response_bytes).hexdigest() != response_sha256:
        raise _manual_artifact_content_mismatch("response content does not match manifest")

    path_manifest = manifest.get("allowed_paths")
    if not isinstance(path_manifest, Mapping):
        raise _manual_artifact_content_mismatch("allowed-path manifest is invalid")
    seen_paths = set()
    for category in ("changed", "new", "deleted"):
        entries = path_manifest.get(category)
        if not isinstance(entries, list):
            raise _manual_artifact_content_mismatch("allowed-path manifest category is invalid")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise _manual_artifact_content_mismatch("allowed-path manifest entry is invalid")
            relative = _validate_artifact_relative(entry.get("path"))
            if relative in seen_paths or not _under_allowed_evidence(relative, allowed_paths):
                raise _manual_artifact_content_mismatch("artifact path is not allowed")
            seen_paths.add(relative)
            if not _is_sha256(entry.get("sha256")):
                raise _manual_artifact_content_mismatch("artifact hash is invalid")
            if category != "deleted":
                artifact_path = candidate / "workspace" / relative
                _require_artifact_file(artifact_path, candidate)
                try:
                    content = artifact_path.read_bytes()
                    content.decode("utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise _manual_artifact_content_mismatch("workspace artifact is unreadable") from exc
                if hashlib.sha256(content).hexdigest() != entry["sha256"]:
                    raise _manual_artifact_content_mismatch("workspace artifact does not match manifest")


def _require_artifact_file(path: Path, artifact_root: Path) -> None:
    lexical = artifact_root / path.relative_to(artifact_root)
    _reject_artifact_symlink_components(artifact_root, lexical)
    try:
        if not lexical.is_file() or lexical.is_symlink():
            raise OSError("not a regular file")
    except OSError as exc:
        raise _manual_artifact_content_mismatch("artifact file is missing or unsafe") from exc


def _reject_artifact_symlink_components(root: Path, path: Path) -> None:
    current = root
    try:
        parts = path.relative_to(root).parts
    except ValueError as exc:
        raise _manual_artifact_content_mismatch("artifact path escaped root") from exc
    for component in parts:
        current = current / component
        if current.is_symlink():
            raise _manual_artifact_content_mismatch("artifact symlink is not allowed")


def _manual_artifact_content_mismatch(detail: str) -> OfficialPilotGateError:
    return OfficialPilotGateError(
        f"manual artifact content mismatch: {detail}",
        error_code="manual_artifact_content_mismatch",
    )


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _materialize_evidence_bundle(
    preflight: OfficialPreflight,
    evidence: Mapping[str, Any],
    execution_records: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[Dict[str, Dict[str, Any]], Path]:
    final_task_root = _artifact_task_root(preflight.artifacts_dir, preflight.task_id)
    if final_task_root.exists():
        raise OfficialPilotGateError(
            "official evidence artifact already exists",
            error_code="artifact_orphan_or_duplicate",
        )
    spec = load_fixture(preflight.fixture_path)
    initial_state = _capture_allowed_path_state(spec.initial_state, spec.allowed_paths)
    plans: Dict[str, Tuple[str, Dict[str, bytes], Dict[str, Any]]] = {}
    total_bytes = 0
    for mode in ("baseline", "router"):
        mode_evidence = evidence.get(mode)
        if not isinstance(mode_evidence, Mapping):
            raise OfficialPilotGateError("official evidence mode is missing", error_code="evidence_missing")
        plan, size = _build_evidence_plan(
            preflight,
            spec.allowed_paths,
            initial_state,
            mode,
            mode_evidence,
            None if execution_records is None else execution_records.get(mode),
        )
        plans[mode] = plan
        total_bytes += size
    if total_bytes > MAX_EVIDENCE_TOTAL_BYTES:
        raise OfficialPilotGateError("official evidence bundle is too large", error_code="evidence_total_too_large")

    preflight.artifacts_dir.mkdir(parents=True, exist_ok=True)
    staging_dir: Optional[Path] = None
    try:
        staging_dir = Path(tempfile.mkdtemp(prefix=f".{preflight.task_id}.", dir=str(preflight.artifacts_dir)))
        attempt_dir = staging_dir / "attempt_1"
        manifest_hashes: Dict[str, str] = {}
        for mode, (response_text, files, manifest) in plans.items():
            mode_dir = attempt_dir / mode
            _write_artifact_file(mode_dir / "response.txt", response_text.encode("utf-8"))
            for relative, content in files.items():
                _write_artifact_file(mode_dir / "workspace" / relative, content)
            manifest_bytes = _json_bytes(manifest)
            _write_artifact_file(mode_dir / "manifest.json", manifest_bytes)
            manifest_hashes[mode] = hashlib.sha256(manifest_bytes).hexdigest()
        _fsync_directory(staging_dir)
        os.replace(staging_dir, final_task_root)
        staging_dir = None
        refs = {
            mode: {
                "evidence_artifact": _relative_project_path(final_task_root / "attempt_1" / mode),
                "evidence_manifest_sha256": manifest_hashes[mode],
            }
            for mode in plans
        }
        return refs, final_task_root
    except OSError as exc:
        raise OfficialPilotGateError(
            "official evidence artifact replacement failed",
            error_code="evidence_artifact_replace_failed",
        ) from exc
    finally:
        if staging_dir is not None:
            try:
                shutil.rmtree(staging_dir)
            except OSError:
                pass


def _build_evidence_plan(
    preflight: OfficialPreflight,
    allowed_paths: Sequence[str],
    initial_state: Mapping[str, Mapping[str, Any]],
    mode: str,
    mode_evidence: Mapping[str, Any],
    execution_record: Optional[Mapping[str, Any]] = None,
) -> Tuple[Tuple[str, Dict[str, bytes], Dict[str, Any]], int]:
    if mode_evidence.get("error"):
        raise OfficialPilotGateError("evidence capture failed", error_code="evidence_capture_failed")
    response = mode_evidence.get("response_text", "")
    if not isinstance(response, str):
        raise OfficialPilotGateError("evidence response is invalid", error_code="evidence_invalid")
    response = _sanitize_artifact_text(response)
    response_bytes = response.encode("utf-8")
    if len(response_bytes) > MAX_EVIDENCE_RESPONSE_BYTES:
        raise OfficialPilotGateError("response evidence is too large", error_code="evidence_response_too_large")
    if not response and isinstance(execution_record, Mapping):
        if execution_record.get("route_status") == "success":
            raise OfficialPilotGateError(
                "successful execution has no normalized response evidence",
                error_code="evidence_response_missing",
            )
    final_state = mode_evidence.get("allowed_paths", {})
    if not isinstance(final_state, Mapping):
        raise OfficialPilotGateError("allowed-path evidence is invalid", error_code="evidence_invalid")

    changed = []
    new = []
    deleted = []
    files: Dict[str, bytes] = {}
    for relative in set(initial_state) | set(final_state):
        safe_relative = _validate_artifact_relative(relative)
        if not _under_allowed_evidence(safe_relative, allowed_paths):
            raise OfficialPilotGateError("evidence path is outside allowed paths", error_code="evidence_path_invalid")
        before = initial_state.get(safe_relative)
        after = final_state.get(safe_relative)
        before_file = _file_state(before)
        after_file = _file_state(after)
        if after_file is None:
            if before_file is not None:
                deleted.append({"path": safe_relative, "sha256": before_file["sha256"]})
            continue
        text = after_file["text"]
        raw_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if after_file.get("sha256") != raw_sha:
            raise OfficialPilotGateError("allowed-path evidence hash mismatch", error_code="evidence_hash_mismatch")
        content = _sanitize_artifact_text(text).encode("utf-8")
        if len(content) > MAX_EVIDENCE_FILE_BYTES:
            raise OfficialPilotGateError("evidence file is too large", error_code="evidence_file_too_large")
        entry = {"path": safe_relative, "sha256": hashlib.sha256(content).hexdigest()}
        if before_file is None:
            new.append(entry)
            files[safe_relative] = content
        elif before_file["sha256"] != raw_sha:
            changed.append(entry)
            files[safe_relative] = content
    manifest = {
        "schema_version": "0.1",
        "task_id": preflight.task_id,
        "attempt": 1,
        "mode": mode,
        "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
        "allowed_paths": {
            "changed": sorted(changed, key=lambda item: item["path"]),
            "new": sorted(new, key=lambda item: item["path"]),
            "deleted": sorted(deleted, key=lambda item: item["path"]),
        },
    }
    return (response, files, manifest), len(response_bytes) + sum(len(value) for value in files.values())


def _file_state(value: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(value, Mapping) or value.get("status") != "present" or value.get("kind") != "file":
        return None
    if not isinstance(value.get("text"), str) or not isinstance(value.get("sha256"), str):
        raise OfficialPilotGateError("allowed-path file evidence is invalid", error_code="evidence_invalid")
    return value


def _sanitize_artifact_text(value: str) -> str:
    key_path = os.environ.get("JEV_API_KEY_FILE")
    for candidate in (key_path, str(Path(key_path).expanduser()) if key_path else None):
        if candidate:
            value = value.replace(candidate, "[REDACTED_KEY_PATH]")
    precise_patterns = (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
        re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\b\s*[:=]\s*(?:['\"][^'\"]*['\"]|[^\s,;]+)"),
        re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{7,}\b"),
        re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/[^\s\"'<>]+"),
    )
    for pattern in precise_patterns:
        value = pattern.sub("[REDACTED]", value)
    return value


def _validate_artifact_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise OfficialPilotGateError("artifact path is not safely relative", error_code="evidence_path_invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts:
        raise OfficialPilotGateError("artifact path is not safely relative", error_code="evidence_path_invalid")
    return pure.as_posix()


def _under_allowed_evidence(path: str, allowed_paths: Sequence[str]) -> bool:
    return any(allowed == "." or path == allowed or path.startswith(allowed + "/") for allowed in allowed_paths)


def _artifact_task_root(artifacts_dir: Path, task_id: str) -> Path:
    return artifacts_dir / task_id


def _artifact_task_dir(artifacts_dir: Path, task_id: str) -> Path:
    return _artifact_task_root(artifacts_dir, task_id) / "attempt_1"


def _attach_artifact_ref(record: Mapping[str, Any], reference: Mapping[str, Any]) -> Dict[str, Any]:
    result = dict(record)
    result.update(reference)
    return result


def _write_artifact_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _relative_project_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise OfficialPilotGateError("artifact path escaped project root", error_code="evidence_path_invalid") from exc


def _remove_artifact_bundle(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise OfficialPilotGateError(
            "new official evidence artifact could not be cleaned up",
            error_code="evidence_cleanup_failed",
        ) from exc


def _run_controlled_task(preflight: OfficialPreflight) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    case_a = run_failure_injection(FailureInjectionCase.FALLBACK_SUCCESS)
    case_b = run_failure_injection(FailureInjectionCase.BUDGET_BLOCK)
    timestamp = _utc_now()
    return (
        _controlled_record(case_a, "baseline", preflight, timestamp),
        _controlled_record(case_b, "router", preflight, timestamp),
    )


def _controlled_record(run: Any, mode: str, preflight: OfficialPreflight, timestamp: str) -> Dict[str, Any]:
    result = run.result
    return _officialize_record(
        {
            "official_pilot": False,
            "task_id": preflight.task_id,
            "mode": mode,
            "status": result.status,
            "route_status": result.status,
            "route_source": result.route_source,
            "selected_model": result.selected_model,
            "classifier": _safe_classifier(result.classifier),
            "classifier_metrics": None,
            "fallback_history": list(result.fallback_history),
            "cost": result.cost.model_dump(mode="json"),
            "error_codes": [error.code for error in result.errors],
            "provider_metrics": {},
            "acceptance_status": "controlled_mock_passed",
            "source_unchanged": True,
            "workspace_removed": None,
            "controlled_mock_case": run.case.value,
            "controlled_mock_summary": run.to_summary(),
        },
        preflight,
        timestamp_utc=timestamp,
        jev_audit={
            "status": "not_applicable",
            "reason": "controlled_mock",
            "classifier": None,
            "experimental_validation_cost": None,
            "assessment": "not_applicable",
            "assessment_status": "not_applicable",
        },
        experimental_validation_cost=None,
    )


def _router_audit(
    router_record: Mapping[str, Any],
    preflight: OfficialPreflight,
    *,
    audit_jev_factory: Optional[Callable[[], Any]],
) -> Tuple[Dict[str, Any], Optional[float]]:
    route_source = router_record.get("route_source")
    if route_source == "jev":
        metrics = router_record.get("classifier_metrics")
        exact_cost = metrics.get("exact_cost") if isinstance(metrics, Mapping) else None
        if exact_cost is None:
            cost = router_record.get("cost")
            exact_cost = cost.get("classifier_cost") if isinstance(cost, Mapping) else None
        if not isinstance(exact_cost, (int, float)) or not math.isfinite(float(exact_cost)):
            raise OfficialPilotGateError("production JEV cost evidence is missing", error_code="jev_cost_missing")
        return (
            {
                "status": "pending_human_review",
                "reason": "production_route_reused",
                "classifier": router_record.get("classifier"),
                "production_classifier_metrics": metrics,
                "experimental_validation_cost": None,
                "assessment": None,
                "assessment_status": "pending_human_review",
                "cost": float(exact_cost),
            },
            None,
        )

    if route_source == "light_rule":
        audit_jev = audit_jev_factory() if audit_jev_factory is not None else RealJEVClassifier()
        spec = load_fixture(preflight.fixture_path)
        request = RouteRequest(task_id=spec.task_id, prompt=spec.prompt, context=[])
        classifier = audit_jev.classify(request)
        cost_component = audit_jev.resolve_cost(request, classifier)
        metrics = _safe_metrics(_last_metrics(audit_jev), "jev")
        exact_cost = metrics.get("exact_cost") if isinstance(metrics, Mapping) else None
        if exact_cost is None:
            exact_cost = getattr(cost_component, "cost", None)
        if not isinstance(exact_cost, (int, float)) or not math.isfinite(float(exact_cost)):
            raise OfficialPilotGateError("audit-only JEV cost evidence is missing", error_code="jev_audit_cost_missing")
        return (
            {
                "status": "pending_human_review",
                "reason": "light_rule_audit_only",
                "classifier": _safe_classifier(classifier),
                "classifier_metrics": metrics,
                "experimental_validation_cost": float(exact_cost),
                "assessment": None,
                "assessment_status": "pending_human_review",
            },
            float(exact_cost),
        )

    return (
        {
            "status": "not_applicable",
            "reason": f"route_source_{route_source or 'unknown'}",
            "classifier": router_record.get("classifier"),
            "experimental_validation_cost": None,
            "assessment": None,
            "assessment_status": "not_applicable",
        },
        None,
    )


def _baseline_audit() -> Dict[str, Any]:
    return {
        "status": "not_applicable",
        "reason": "baseline_no_jev",
        "classifier": None,
        "experimental_validation_cost": None,
        "assessment": None,
        "assessment_status": "not_applicable",
    }


def _officialize_record(
    record: Mapping[str, Any],
    preflight: OfficialPreflight,
    *,
    timestamp_utc: Optional[str],
    jev_audit: Mapping[str, Any],
    experimental_validation_cost: Optional[float],
) -> Dict[str, Any]:
    result = dict(record)
    result.update(
        {
            "official_pilot": True,
            "attempt": 1,
            "config_schema_version": preflight.config.schema_version,
            "config_snapshot": _config_snapshot(preflight.config),
            "timestamp_utc": timestamp_utc or _utc_now(),
            "source_fixture_hash": preflight.source_fixture_hash,
            "jev_audit": dict(jev_audit),
            "experimental_validation_cost": experimental_validation_cost,
        }
    )
    if (
        preflight.acceptance_mode == "manual"
        and result.get("controlled_mock_case") is None
    ):
        result["acceptance_status"] = "pending_manual"
    _reject_unsafe_record(result)
    return result


def _config_snapshot(config: PilotConfig) -> Dict[str, Any]:
    return {
        "schema_version": config.schema_version,
        "baseline_model": config.baseline_model,
        "budget_limit_per_real_task": config.budget_limit_per_real_task,
        "estimated_max_costs": dict(config.estimated_max_costs),
        "budget_contract": config.budget_contract,
        "audit_timing": config.audit_timing,
        "audit_cost_field": config.audit_cost_field,
        "audit_cost_included_in_production_route_cost": config.audit_cost_included_in_production_route_cost,
        "controlled_failure_task_id": config.controlled_failure_task_id,
    }


def _validate_bridge_pair(pair: Mapping[str, Any], task_id: str) -> None:
    if not isinstance(pair, Mapping) or pair.get("official_pilot") is not False:
        raise OfficialPilotGateError("real-pilot bridge returned an unsafe pair", error_code="bridge_pair_invalid")
    for mode in ("baseline", "router"):
        record = pair.get(mode)
        if not isinstance(record, Mapping) or record.get("task_id") != task_id or record.get("mode") != mode:
            raise OfficialPilotGateError("real-pilot bridge returned an incomplete pair", error_code="bridge_pair_incomplete")
        _reject_unsafe_record(record)


def _validate_official_pair(baseline: Mapping[str, Any], router: Mapping[str, Any], task_id: str) -> None:
    for mode, record in (("baseline", baseline), ("router", router)):
        if not isinstance(record, Mapping):
            raise OfficialPilotGateError("official pair contains a non-record", error_code="pair_invalid")
        if record.get("official_pilot") is not True or record.get("attempt") != 1:
            raise OfficialPilotGateError("official pair authorization fields are invalid", error_code="pair_authorization_invalid")
        if record.get("task_id") != task_id or record.get("mode") != mode:
            raise OfficialPilotGateError("official pair task or mode is invalid", error_code="pair_identity_invalid")
        _reject_unsafe_record(record)


def _read_official_records(path: Path) -> Sequence[Mapping[str, Any]]:
    return _decode_official_records(_read_official_bytes(path))


def _read_official_bytes(path: Path) -> bytes:
    if not path.exists():
        return b""
    if not path.is_file():
        raise OfficialPilotGateError("official result path is not a file", error_code="official_results_not_file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise OfficialPilotGateError("official result file is unreadable", error_code="official_results_unreadable") from exc
    if not raw or not raw.endswith(b"\n"):
        raise OfficialPilotGateError("official JSONL is malformed or truncated", error_code="official_results_malformed")
    return raw


def _decode_official_records(raw: bytes) -> Sequence[Mapping[str, Any]]:
    if not raw:
        return []
    records = []
    for line in raw.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise OfficialPilotGateError("official JSONL is malformed or truncated", error_code="official_results_malformed") from None
        if not isinstance(value, dict):
            raise OfficialPilotGateError("official JSONL record is not an object", error_code="official_results_malformed")
        _reject_unsafe_record(value)
        if value.get("official_pilot") is not True:
            raise OfficialPilotGateError("official result file contains a non-official record", error_code="mixed_official_results")
        if value.get("attempt") != 1:
            raise OfficialPilotGateError("repeat attempts are not authorized", error_code="repeat_attempt_forbidden")
        if value.get("task_id") not in (*EXPECTED_REAL_TASK_IDS, CONTROLLED_FAILURE_TASK_ID):
            raise OfficialPilotGateError("official result has an unsupported task id", error_code="official_task_invalid")
        if value.get("mode") not in {"baseline", "router"}:
            raise OfficialPilotGateError("official result has an unsupported mode", error_code="official_mode_invalid")
        records.append(value)
    return records


def _serialize_official_record(record: Mapping[str, Any]) -> bytes:
    _reject_non_finite(record)
    try:
        return (
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
        raise OfficialPilotGateError(
            "official record is not JSON serializable",
            error_code="official_record_not_serializable",
        ) from exc


def _atomic_replace_official_jsonl(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    descriptor: Optional[int] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise OfficialPilotGateError(
            "official pair atomic replacement failed",
            error_code="official_pair_replace_failed",
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _next_allowed_task(records: Sequence[Mapping[str, Any]], config: PilotConfig) -> str:
    grouped: Dict[str, Dict[str, int]] = {}
    for record in records:
        task_id = record["task_id"]
        mode = record["mode"]
        grouped.setdefault(task_id, {})[mode] = grouped.setdefault(task_id, {}).get(mode, 0) + 1
        if grouped[task_id][mode] != 1:
            raise OfficialPilotGateError("duplicate official first-attempt record", error_code="duplicate_official_record")

    ordered = (*config.real_task_ids, config.controlled_failure_task_id)
    for task in ordered:
        task_records = [record for record in records if record.get("task_id") == task]
        if (
            _task_has_complete_pair(records, task)
            and any(record.get("acceptance_status") == "pending_manual" for record in task_records)
        ):
            raise OfficialPilotGateError(
                "manual acceptance is pending",
                error_code="manual_acceptance_pending",
            )
    for index, task in enumerate(ordered):
        modes = set(grouped.get(task, {}))
        if modes and modes != {"baseline", "router"}:
            raise OfficialPilotGateError("official pair is incomplete", error_code="incomplete_official_pair")
        if not modes:
            if any(grouped.get(later) for later in ordered[index + 1 :]):
                raise OfficialPilotGateError("official results are out of order", error_code="task_order_violation")
            return task
    raise OfficialPilotGateError("all official task slots are complete", error_code="pilot_complete")


def _task_has_complete_pair(records: Sequence[Mapping[str, Any]], task_id: str) -> bool:
    modes = {record.get("mode") for record in records if record.get("task_id") == task_id}
    return modes == {"baseline", "router"}


def _validate_workflow_state(path: Union[str, Path]) -> str:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfficialPilotGateError("workflow state is not loadable", error_code="workflow_state_unloadable") from exc
    preparation = state.get("pilot_preparation") if isinstance(state, dict) else None
    if not isinstance(state, dict) or not isinstance(preparation, dict):
        raise OfficialPilotGateError("workflow state is not valid", error_code="workflow_state_invalid")
    phase_a = (
        state.get("status") == "pilot_config_frozen"
        and state.get("current_task") == "PILOT_OFFICIAL_EXECUTION_GATE"
        and preparation.get("execution_gate") == "official_execution_mode_required"
        and preparation.get("real_pilot_started") is False
    )
    phase_b = (
        state.get("status") == "official_pilot_running"
        and state.get("current_task") == "PILOT_OFFICIAL_EXECUTION"
        and preparation.get("execution_gate") == "official_pilot_in_progress"
        and preparation.get("real_pilot_started") is True
    )
    if phase_a:
        return "before_first_official_run"
    if phase_b:
        return "official_pilot_running"
    raise OfficialPilotGateError("workflow is not in an allowed official execution phase", error_code="workflow_gate_not_ready")


def _validate_phase_results(
    phase: str,
    records: Sequence[Mapping[str, Any]],
    config: PilotConfig,
) -> None:
    complete_pairs = sum(
        _task_has_complete_pair(records, task_id)
        for task_id in (*config.real_task_ids, config.controlled_failure_task_id)
    )
    if phase == "before_first_official_run" and records:
        raise OfficialPilotGateError(
            "pre-first-run workflow phase cannot contain official results",
            error_code="workflow_results_mismatch",
        )
    if phase == "official_pilot_running" and complete_pairs == 0:
        raise OfficialPilotGateError(
            "running workflow phase requires at least one complete official pair",
            error_code="workflow_results_mismatch",
        )
    if phase == "official_pilot_running" and complete_pairs >= config.total_task_slots:
        raise OfficialPilotGateError(
            "running workflow phase cannot represent a completed Pilot",
            error_code="workflow_results_mismatch",
        )


def _validate_output_paths(config: PilotConfig) -> Dict[str, Path]:
    values = {
        "official_results_path": config.outputs.official_results_path,
        "summary_json_path": config.outputs.summary_json_path,
        "summary_markdown_path": config.outputs.summary_markdown_path,
        "artifacts_dir": config.outputs.artifacts_dir,
    }
    results_root = (PROJECT_ROOT / "benchmark" / "results").resolve()
    resolved: Dict[str, Path] = {}
    for name, relative in values.items():
        candidate = (PROJECT_ROOT / relative).resolve()
        try:
            candidate.relative_to(results_root)
        except ValueError:
            raise OfficialPilotGateError("configured output is outside benchmark/results", error_code="output_outside_results") from None
        resolved[name] = candidate
    return resolved


def _require_jev_key_file() -> None:
    raw = os.environ.get("JEV_API_KEY_FILE")
    if not raw:
        raise OfficialPilotGateError("JEV_API_KEY_FILE is required for real tasks", error_code="jev_api_key_file_required")
    path = Path(raw).expanduser()
    if not path.is_file() or not os.access(path, os.R_OK):
        raise OfficialPilotGateError("JEV_API_KEY_FILE must point to a readable file", error_code="jev_api_key_file_invalid")


def _resolve_path(path: Union[str, Path]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _validate_task_id(task_id: str) -> None:
    if task_id not in (*EXPECTED_REAL_TASK_IDS, CONTROLLED_FAILURE_TASK_ID):
        raise OfficialPilotGateError("task must be one exact official task id", error_code="task_id_invalid")


def _require_authorization(authorization: Any) -> None:
    if (
        not isinstance(authorization, _OfficialExecutionAuthorization)
        or authorization._auth_token is not _AUTH_TOKEN
        or authorization._append_token is not _OFFICIAL_APPEND_TOKEN
    ):
        raise OfficialPilotGateError("explicit official authorization is required", error_code="authorization_required")


def _require_preflight(task_id: str, preflight: Any) -> None:
    if not isinstance(preflight, OfficialPreflight) or preflight._proof is not _PREFLIGHT_TOKEN or preflight.task_id != task_id:
        raise OfficialPilotGateError("successful official preflight is required", error_code="preflight_required")


def _refresh_preflight(preflight: OfficialPreflight) -> None:
    current = preflight_official_task(
        preflight.task_id,
        config_path=preflight.config_path,
        workflow_state_path=preflight.workflow_state_path,
        registry_path=preflight.registry_path,
        fixture_root=preflight.fixture_root,
    )
    if current.source_fixture_hash != preflight.source_fixture_hash or current.results_path != preflight.results_path:
        raise OfficialPilotGateError("official preflight is stale", error_code="preflight_stale")


def _reject_unsafe_record(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                raise OfficialPilotGateError("record contains forbidden raw or secret data", error_code="unsafe_record")
            _reject_unsafe_record(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_unsafe_record(item)
    elif isinstance(value, str) and _SECRET_TEXT.search(value):
        raise OfficialPilotGateError("record contains forbidden secret-like text", error_code="unsafe_record")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONFIRMATION_TEXT",
    "MANUAL_REVIEW_CONFIRMATION_TEXT",
    "OfficialPilotGateError",
    "OfficialPreflight",
    "execute_official_task",
    "finalize_manual_acceptance",
    "persist_official_pair",
    "preflight_official_task",
]
