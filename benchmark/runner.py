"""T-12 benchmark fixture loader and isolated execution harness.

The harness deliberately knows nothing about model providers or the Router.
Both task executors are injected callables.  A run receives a disposable copy
of ``initial_state`` and all acceptance commands run with that copy as their
working directory.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from ast import literal_eval
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union


_REQUIRED_FILES = (
    "task.yaml",
    "prompt.md",
    "acceptance.md",
    "expected_constraints.md",
)
_OUTPUT_LIMIT = 4096
ACCEPTANCE_TIMEOUT_SECONDS = 300
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+[^\s,;]+"),
    re.compile(r"(?i)\bbearer\s+[^\s,;]+"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*"
        r"[\"']?[^\s,;\"']+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{7,}\b"),
)


class FixtureValidationError(ValueError):
    """Raised when a fixture violates the documented T-12 contract."""

    code = "fixture_validation_error"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


@dataclass(frozen=True)
class FixtureSpec:
    """Validated, read-only view of one benchmark fixture."""

    path: Path
    task_id: str
    baseline_model: str
    task_metadata: Mapping[str, Any]
    prompt: str
    acceptance_text: str
    acceptance_mode: str
    acceptance_command: Optional[Tuple[str, ...]]
    allowed_paths: Tuple[str, ...]
    allowed_paths_explicit: bool
    expected_paths: Tuple[str, ...]
    forbidden_paths: Tuple[str, ...]
    initial_state: Path


def load_fixture(path: Union[os.PathLike, str]) -> FixtureSpec:
    """Load and validate the five-file fixture contract."""

    fixture_path = Path(path)
    try:
        fixture_path = fixture_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise FixtureValidationError("fixture directory is not readable") from exc
    if not fixture_path.is_dir():
        raise FixtureValidationError("fixture path must be a directory")

    _reject_symlinks(fixture_path)
    for name in _REQUIRED_FILES:
        candidate = fixture_path / name
        if not candidate.is_file():
            raise FixtureValidationError(f"missing required file: {name}")

    initial_state = fixture_path / "initial_state"
    if not initial_state.is_dir():
        raise FixtureValidationError("initial_state must be a directory")

    try:
        task_config = _parse_yaml((fixture_path / "task.yaml").read_text(encoding="utf-8"))
        prompt = (fixture_path / "prompt.md").read_text(encoding="utf-8")
        acceptance_text = (fixture_path / "acceptance.md").read_text(encoding="utf-8")
        constraints_text = (
            fixture_path / "expected_constraints.md"
        ).read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise FixtureValidationError("fixture files are not readable") from exc

    if not isinstance(task_config, dict):
        raise FixtureValidationError("task.yaml must contain a mapping")
    task_id = _required_string(task_config, "id")
    baseline_model = _required_string(task_config, "baseline_model")
    if not prompt.strip():
        raise FixtureValidationError("prompt.md must not be empty")

    acceptance = task_config.get("acceptance")
    if not isinstance(acceptance, dict):
        raise FixtureValidationError("acceptance must be a mapping")
    mode = acceptance.get("mode")
    if not isinstance(mode, str) or mode.strip().lower() not in {"automated", "manual"}:
        raise FixtureValidationError("acceptance.mode must be automated or manual")
    mode = mode.strip().lower()

    command = _parse_command(acceptance.get("command")) if mode == "automated" else None
    allowed_paths = _paths_from_mapping(acceptance, "allowed_paths")
    expected_paths, forbidden_paths = _constraint_paths(task_config, acceptance)
    doc_expected, doc_forbidden = _parse_constraint_document(constraints_text)
    expected_paths = _unique(expected_paths + doc_expected)
    forbidden_paths = _unique(forbidden_paths + doc_forbidden)
    _reject_overlapping_constraints(expected_paths, forbidden_paths)

    return FixtureSpec(
        path=fixture_path,
        task_id=task_id,
        baseline_model=baseline_model,
        task_metadata=_json_safe_copy(task_config),
        prompt=prompt,
        acceptance_text=acceptance_text,
        acceptance_mode=mode,
        acceptance_command=command,
        allowed_paths=allowed_paths,
        allowed_paths_explicit="allowed_paths" in acceptance,
        expected_paths=expected_paths,
        forbidden_paths=forbidden_paths,
        initial_state=initial_state,
    )


def run_fixture(
    path: Union[os.PathLike, str],
    mode: str,
    baseline_executor: Optional[Callable[[Path, Mapping[str, Any], str], Any]] = None,
    router_executor: Optional[Callable[[Path, Mapping[str, Any], str], Any]] = None,
    command_runner: Optional[Callable[..., Any]] = None,
    post_run_hook: Optional[Callable[[Path, FixtureSpec], Any]] = None,
) -> Dict[str, Any]:
    """Run one fixture in a fresh temporary workspace.

    Executors receive ``(workspace, task_metadata, prompt)``.  The default
    acceptance runner receives an argv sequence and uses ``shell=False``.
    """

    if mode not in {"baseline", "router"}:
        raise ValueError("mode must be baseline or router")
    spec = load_fixture(path)
    executor = baseline_executor if mode == "baseline" else router_executor
    source_hash_before = _tree_hash(spec.path)
    workspace_initial_hash = _tree_hash(spec.initial_state)

    executor_summary: Dict[str, Any] = {"status": "not_run"}
    errors: List[Dict[str, str]] = []
    acceptance: Dict[str, Any]
    run_status = "failed"
    workspace_path: Optional[Path] = None

    try:
        with tempfile.TemporaryDirectory(prefix="jev-benchmark-") as temp_root:
            workspace_path = Path(temp_root) / "workspace"
            shutil.copytree(spec.initial_state, workspace_path, symlinks=False)
            if _tree_hash(workspace_path) != workspace_initial_hash:
                errors.append(
                    {"code": "workspace_snapshot_mismatch", "message": "Workspace snapshot mismatch"}
                )
            elif executor is None:
                errors.append(
                    {"code": "executor_not_configured", "message": "Executor not configured"}
                )
            else:
                try:
                    value = executor(
                        workspace_path,
                        _json_safe_copy(spec.task_metadata),
                        spec.prompt,
                    )
                    executor_summary = _executor_summary(value)
                except Exception:
                    errors.append(
                        {"code": "executor_failed", "message": "Executor failed"}
                    )

                if not errors:
                    acceptance = _run_acceptance(
                        spec,
                        workspace_path,
                        command_runner=command_runner,
                    )
                    if acceptance["status"] == "passed":
                        run_status = "passed"
                    elif acceptance["status"] == "pending_manual":
                        run_status = "pending_manual"
                    else:
                        run_status = "failed"
                else:
                    acceptance = _not_run_acceptance(spec)
                if post_run_hook is not None:
                    try:
                        post_run_hook(workspace_path, spec)
                    except Exception:
                        errors.append(
                            {"code": "post_run_hook_failed", "message": "Post-run hook failed"}
                        )
                        run_status = "failed"
    except (OSError, shutil.Error):
        errors.append(
            {"code": "workspace_setup_failed", "message": "Workspace setup failed"}
        )
        acceptance = _not_run_acceptance(spec)

    source_hash_after: Optional[str]
    try:
        source_hash_after = _tree_hash(spec.path)
    except (OSError, ValueError):
        source_hash_after = None
    source_unchanged = source_hash_after == source_hash_before
    if not source_unchanged:
        errors.append(
            {"code": "fixture_modified", "message": "Source fixture changed during run"}
        )
        run_status = "failed"

    if errors:
        run_status = "failed"
        if acceptance.get("status") == "passed":
            acceptance = dict(acceptance)
            acceptance["status"] = "failed"

    return {
        "task_id": spec.task_id,
        "mode": mode,
        "status": run_status,
        "baseline_model": spec.baseline_model,
        "workspace_initial_hash": workspace_initial_hash,
        "source_fixture_hash_before": source_hash_before,
        "source_fixture_hash_after": source_hash_after,
        "source_unchanged": source_unchanged,
        "workspace_removed": workspace_path is not None and not workspace_path.exists(),
        "acceptance": acceptance,
        "executor": executor_summary,
        "errors": errors,
    }


def run_pair(
    path: Union[os.PathLike, str],
    baseline_executor: Callable[[Path, Mapping[str, Any], str], Any],
    router_executor: Callable[[Path, Mapping[str, Any], str], Any],
    command_runner: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Run baseline and router independently and return a minimal summary."""

    spec = load_fixture(path)
    baseline = run_fixture(
        spec.path,
        "baseline",
        baseline_executor=baseline_executor,
        command_runner=command_runner,
    )
    router = run_fixture(
        spec.path,
        "router",
        router_executor=router_executor,
        command_runner=command_runner,
    )
    return {
        "task_id": spec.task_id,
        "baseline": _minimal_summary(baseline),
        "router": _minimal_summary(router),
    }


def _run_acceptance(
    spec: FixtureSpec,
    workspace: Path,
    *,
    command_runner: Optional[Callable[..., Any]],
) -> Dict[str, Any]:
    if spec.acceptance_mode == "manual":
        checks = _filesystem_checks(spec, workspace)
        passed = all(check["passed"] for check in checks)
        return {
            "mode": "manual",
            "status": "pending_manual" if passed else "failed",
            "passed": None if passed else False,
            "criteria": _safe_text(spec.acceptance_text),
            "checks": checks,
        }

    assert spec.acceptance_command is not None
    argv = spec.acceptance_command
    runner = command_runner or _default_command_runner
    checks: List[Dict[str, Any]] = []
    try:
        completed = runner(
            argv,
            cwd=workspace,
            shell=False,
            capture_output=True,
            text=True,
            env=_safe_environment(),
            check=False,
            timeout=ACCEPTANCE_TIMEOUT_SECONDS,
        )
        exit_code = _safe_exit_code(getattr(completed, "returncode", None))
        stdout = _safe_text(getattr(completed, "stdout", ""))
        stderr = _safe_text(getattr(completed, "stderr", ""))
        command_failed = exit_code is None or exit_code != 0
        if command_failed:
            checks.insert(
                0,
                {"name": "command", "passed": False, "exit_code": exit_code},
            )
        else:
            checks.insert(0, {"name": "command", "passed": True, "exit_code": exit_code})
        checks.extend(_filesystem_checks(spec, workspace))
        passed = all(check["passed"] for check in checks)
        return {
            "mode": "automated",
            "status": "passed" if passed else "failed",
            "passed": passed,
            "argv": [_safe_text(item) for item in argv],
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "checks": checks,
        }
    except subprocess.TimeoutExpired:
        return {
            "mode": "automated",
            "status": "failed",
            "passed": False,
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error_code": "acceptance_command_timeout",
            "checks": [{"name": "command", "passed": False, "exit_code": None}],
        }
    except Exception:
        return {
            "mode": "automated",
            "status": "failed",
            "passed": False,
            "argv": [_safe_text(item) for item in argv],
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error_code": "acceptance_command_failed",
            "checks": [{"name": "command", "passed": False, "exit_code": None}],
        }


def _filesystem_checks(spec: FixtureSpec, workspace: Path) -> List[Dict[str, Any]]:
    before = _tree_snapshot(spec.initial_state)
    after = _tree_snapshot(workspace)
    changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
    ignored_runtime_paths = sorted(path for path in changed if _is_acceptance_runtime_noise(path))
    material_changed = [path for path in changed if path not in ignored_runtime_paths]
    checks: List[Dict[str, Any]] = []
    if spec.allowed_paths:
        disallowed = [
            path for path in material_changed if not _under_allowed(path, spec.allowed_paths)
        ]
        checks.append(
            {
                "name": "allowed_paths",
                "passed": not disallowed,
                "changed_paths": changed,
                "material_changed_paths": material_changed,
                "ignored_runtime_paths": ignored_runtime_paths,
                "disallowed_paths": disallowed,
            }
        )
    elif spec.allowed_paths == () and spec.allowed_paths_explicit:
        checks.append(
            {
                "name": "allowed_paths",
                "passed": not changed,
                "changed_paths": changed,
                "disallowed_paths": changed,
            }
        )
    if spec.expected_paths:
        missing = [path for path in spec.expected_paths if not _exists(workspace / path)]
        checks.append(
            {"name": "expected_paths", "passed": not missing, "missing_paths": missing}
        )
    if spec.forbidden_paths:
        present = [path for path in spec.forbidden_paths if _exists(workspace / path)]
        checks.append(
            {"name": "forbidden_paths", "passed": not present, "present_paths": present}
        )
    return checks


def _not_run_acceptance(spec: FixtureSpec) -> Dict[str, Any]:
    return {
        "mode": spec.acceptance_mode,
        "status": "not_run",
        "passed": None,
        "checks": [],
    }


def _minimal_summary(result: Mapping[str, Any]) -> Dict[str, Any]:
    acceptance = result["acceptance"]
    return {
        "mode": result["mode"],
        "status": result["status"],
        "workspace_initial_hash": result["workspace_initial_hash"],
        "acceptance": {
            "status": acceptance["status"],
            "passed": acceptance.get("passed"),
        },
        "executor": result["executor"],
    }


def _executor_summary(value: Any) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "status": "completed",
        "result_type": type(value).__name__,
    }
    if isinstance(value, Mapping):
        keys = sorted(str(key) for key in value.keys())[:32]
        summary["result_keys"] = [_safe_text(key) for key in keys]
        status = value.get("status")
        if isinstance(status, str) and status:
            summary["status"] = _safe_text(status)
    return summary


def _default_command_runner(argv: Sequence[str], **kwargs: Any) -> Any:
    return subprocess.run(argv, **kwargs)


def _safe_environment() -> Dict[str, str]:
    path = os.environ.get("PATH", "")
    return {
        "PATH": path,
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONIOENCODING": "utf-8",
    }


def _safe_text(value: Any) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    elif not isinstance(value, str):
        value = "" if value is None else str(value)
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    if len(value) > _OUTPUT_LIMIT:
        marker = "[TRUNCATED]"
        return value[: _OUTPUT_LIMIT - len(marker)] + marker
    return value


def _safe_exit_code(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _under_allowed(path: str, allowed_paths: Sequence[str]) -> bool:
    for allowed in allowed_paths:
        if allowed == ".":
            return True
        if path == allowed or path.startswith(allowed + "/"):
            return True
        # Parent directories that must exist to hold an allowed file or tree.
        if allowed.startswith(path + "/"):
            return True
    return False


def _is_acceptance_runtime_noise(path: str) -> bool:
    """Return True for Python/pytest artifacts that acceptance commands may create.

    These paths are excluded from allowed_paths enforcement only when the fixture
    declares a non-empty allowed_paths list.  Empty allowed_paths (``[]``) keeps
    the strict no-filesystem-change contract unchanged.
    """

    if not path:
        return False
    parts = PurePosixPath(path).parts
    if ".pytest_cache" in parts:
        return True
    if "__pycache__" in parts:
        return True
    return path.endswith(".pyc") or path.endswith(".pyo")


def _tree_hash(root: Path) -> str:
    snapshot = _tree_snapshot(root)
    digest = hashlib.sha256()
    for name in sorted(snapshot):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(snapshot[name].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _tree_snapshot(root: Path) -> Dict[str, str]:
    if not root.is_dir():
        raise OSError("tree root is not a directory")
    snapshot: Dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directories):
            item = current_path / name
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                snapshot[relative] = "L:" + os.readlink(item)
            else:
                snapshot[relative] = "D"
        for name in sorted(files):
            item = current_path / name
            relative = item.relative_to(root).as_posix()
            if item.is_symlink():
                snapshot[relative] = "L:" + os.readlink(item)
                continue
            file_digest = hashlib.sha256()
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    file_digest.update(chunk)
            snapshot[relative] = "F:" + file_digest.hexdigest()
    return snapshot


def _reject_symlinks(root: Path) -> None:
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        if any((current_path / name).is_symlink() for name in directories + files):
            raise FixtureValidationError("fixture symlinks are not allowed")


def _required_string(mapping: Mapping[str, Any], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FixtureValidationError(f"{key} must be a non-empty string")
    return value.strip()


def _parse_command(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        try:
            values = shlex.split(value, posix=True)
        except ValueError as exc:
            raise FixtureValidationError("acceptance.command is not valid shell syntax") from exc
    elif isinstance(value, list):
        values = value
    else:
        raise FixtureValidationError("automated acceptance.command is required")
    if not values or any(not isinstance(item, str) for item in values) or not values[0]:
        raise FixtureValidationError("acceptance.command must contain a command")
    return tuple(values)


def _paths_from_mapping(mapping: Mapping[str, Any], key: str) -> Tuple[str, ...]:
    if key not in mapping:
        return ()
    value = mapping[key]
    if not isinstance(value, list):
        raise FixtureValidationError(f"{key} must be a list")
    return tuple(_validate_relative_path(item) for item in value)


def _constraint_paths(
    task_config: Mapping[str, Any], acceptance: Mapping[str, Any]
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    expected: List[str] = []
    forbidden: List[str] = []
    containers = [task_config, acceptance]
    for key in ("constraints", "expected_constraints"):
        value = task_config.get(key)
        if isinstance(value, dict):
            containers.append(value)
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        for key in ("expected_paths", "expected_files", "required_paths"):
            expected.extend(_paths_from_mapping(container, key))
        for key in ("forbidden_paths", "forbidden_files"):
            forbidden.extend(_paths_from_mapping(container, key))
    return _unique(expected), _unique(forbidden)


def _parse_constraint_document(text: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    expected: List[str] = []
    forbidden: List[str] = []
    section: Optional[str] = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if re.match(
            r"^#{0,6}\s*expected(?:[_ ](?:paths?|files?|changes?|constraints?))?\s*:?\s*$",
            lower,
        ):
            section = "expected"
            continue
        if re.match(
            r"^#{0,6}\s*forbidden(?:[_ ](?:paths?|files?|changes?|constraints?))?\s*:?\s*$",
            lower,
        ):
            section = "forbidden"
            continue
        explicit = re.match(
            r"^(?:[-*]\s*)?(expected|forbidden)"
            r"(?:[_ ](?:paths?|files?|changes?|constraints?))?\s*:\s*(.+)$",
            line,
            re.I,
        )
        if explicit:
            _append_constraint(explicit.group(1), explicit.group(2), expected, forbidden)
            continue
        if section and line.startswith(("-", "*")):
            value = line[1:].strip()
            value = re.sub(r"^\[[ xX]\]\s*", "", value)
            if value and not value.startswith("["):
                _append_constraint(section, value, expected, forbidden)
    return _unique(expected), _unique(forbidden)


def _append_constraint(kind: str, value: str, expected: List[str], forbidden: List[str]) -> None:
    value = value.strip().strip("`\"'")
    if not value:
        raise FixtureValidationError("constraint path must not be empty")
    normalized = _validate_relative_path(value)
    (expected if kind.lower() == "expected" else forbidden).append(normalized)


def _reject_overlapping_constraints(expected: Sequence[str], forbidden: Sequence[str]) -> None:
    overlap = set(expected) & set(forbidden)
    if overlap:
        raise FixtureValidationError("expected and forbidden paths overlap")


def _validate_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FixtureValidationError("constraint path must be a non-empty string")
    raw = value.strip().replace("\\", "/")
    if ntpath.splitdrive(raw)[0] or raw.startswith("/") or raw == "~" or raw.startswith("~/"):
        raise FixtureValidationError("constraint path must be relative")
    parts = PurePosixPath(raw).parts
    if any(part == ".." for part in parts):
        raise FixtureValidationError("constraint path must not traverse parent directories")
    normalized = "/".join(part for part in parts if part not in {"", "."})
    return normalized or "."


def _unique(values: Sequence[str]) -> Tuple[str, ...]:
    result: List[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


def _json_safe_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError):
        raise FixtureValidationError("task metadata must be JSON serializable") from None


def _parse_yaml(text: str) -> Any:
    """Parse the small scalar/mapping/list YAML subset used by fixtures."""

    lines = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if "\t" in raw_line:
            raise ValueError(f"tabs are not supported (line {line_number})")
        content = _strip_comment(raw_line).rstrip()
        if not content.strip():
            continue
        indentation = len(content) - len(content.lstrip(" "))
        lines.append((line_number, indentation, content[indentation:]))
    if not lines:
        raise ValueError("configuration is empty")
    value, position = _parse_block(lines, 0, lines[0][1])
    if position != len(lines):
        raise ValueError(f"unexpected indentation (line {lines[position][0]})")
    return value


def _parse_block(lines: Sequence[Tuple[int, int, str]], position: int, indentation: int) -> Tuple[Any, int]:
    is_list = lines[position][1] == indentation and lines[position][2].startswith("-")
    result: Any = [] if is_list else {}
    while position < len(lines):
        line_number, current_indent, content = lines[position]
        if current_indent < indentation:
            break
        if current_indent > indentation:
            raise ValueError(f"unexpected indentation (line {line_number})")
        if is_list:
            if not content.startswith("-") or (len(content) > 1 and not content[1].isspace()):
                raise ValueError(f"expected list item (line {line_number})")
            item = content[1:].strip()
            if not item:
                raise ValueError(f"list item must have a value (line {line_number})")
            result.append(_parse_scalar(item))
            position += 1
            continue
        if content.startswith("-") or ":" not in content:
            raise ValueError(f"expected mapping entry (line {line_number})")
        key, raw_value = content.split(":", 1)
        key = key.strip()
        if not key or key in result:
            raise ValueError(f"invalid or duplicate mapping key (line {line_number})")
        raw_value = raw_value.strip()
        position += 1
        if raw_value:
            result[key] = _parse_scalar(raw_value)
            continue
        if position >= len(lines) or lines[position][1] <= indentation:
            raise ValueError(f"mapping key has no value (line {line_number})")
        result[key], position = _parse_block(lines, position, lines[position][1])
    return result, position


def _parse_scalar(value: str) -> Any:
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = literal_eval(value)
        except (ValueError, SyntaxError):
            return [_parse_scalar(item.strip()) for item in value[1:-1].split(",") if item.strip()]
        return parsed
    try:
        return literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def _strip_comment(value: str) -> str:
    quote: Optional[str] = None
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            quote = None if quote == character else character if quote is None else quote
        elif character == "#" and quote is None:
            return value[:index]
    return value
