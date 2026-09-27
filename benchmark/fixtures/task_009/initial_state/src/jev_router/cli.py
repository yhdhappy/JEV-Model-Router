"""T-11 command-line boundary for the JEV Model Router.

This module deliberately stops at CLI orchestration.  Fixture execution and
pilot execution are injected through handlers so T-12 can add those semantics
without making this boundary read credentials, use the network, or own a
benchmark implementation.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from .errors import ConfigurationError
from .registry import ModelRegistry


EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_CONFIGURATION = 3
EXIT_EXECUTION = 4
EXIT_RESULT_FILE = 5

RESULT_SCHEMA_VERSION = "0.1"
DEFAULT_MODELS_PATH = Path("config/models.yaml")

_CONFIGURATION_ERROR = "configuration_error"
_ARGUMENT_ERROR = "argument_error"
_EXECUTION_NOT_CONFIGURED = "execution_not_configured"
_EXECUTION_FAILED = "execution_failed"
_RESULT_FILE_ERROR = "result_file_error"
_RESULT_SERIALIZATION_ERROR = "result_serialization_error"

RunHandler = Callable[[Path, Optional[str]], Any]
PilotHandler = Callable[[Path], Any]


class _ArgumentParser(argparse.ArgumentParser):
    """Parser kept as a normal return-code boundary for ``main`` tests."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        lowered = message.lower()
        if "required" in lowered:
            safe_message = "required argument missing"
        elif "must not be empty" in lowered:
            safe_message = "path must not be empty"
        elif "invalid choice" in lowered:
            safe_message = "unknown command"
        else:
            safe_message = "invalid arguments"
        self._print_message(f"{self.prog}: error: {safe_message}\n", sys.stderr)
        raise SystemExit(EXIT_USAGE)


def _path_argument(value: str) -> Path:
    if not value:
        raise argparse.ArgumentTypeError("path must not be empty")
    return Path(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="python -m jev_router.cli",
        description="JEV Model Router stage-1 CLI",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser(
        "validate-config", help="validate the model registry configuration"
    )
    validate.add_argument(
        "--models",
        type=_path_argument,
        default=DEFAULT_MODELS_PATH,
        metavar="PATH",
        help="model registry YAML path (default: config/models.yaml)",
    )
    validate.add_argument(
        "--result-file",
        type=_path_argument,
        metavar="PATH",
        help="optional structured JSON result path",
    )

    run = commands.add_parser("run", help="delegate one fixture execution")
    run.add_argument(
        "--fixture",
        required=True,
        type=_path_argument,
        metavar="PATH",
        help="existing fixture path",
    )
    run.add_argument("--model", metavar="MODEL", help="optional manual model")
    run.add_argument(
        "--result-file",
        type=_path_argument,
        metavar="PATH",
        help="optional structured JSON result path",
    )

    pilot = commands.add_parser("pilot", help="delegate one pilot configuration")
    pilot.add_argument(
        "--config",
        required=True,
        type=_path_argument,
        metavar="PATH",
        help="existing pilot configuration path",
    )
    pilot.add_argument(
        "--result-file",
        type=_path_argument,
        metavar="PATH",
        help="optional structured JSON result path",
    )
    return parser


def _record(
    command: str,
    status: str,
    error_code: Optional[str],
    **fields: Any,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "command": command,
        "status": status,
        "error_code": error_code,
    }
    record.update(fields)
    return record


def _serialize_record(record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    try:
        payload = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None, _RESULT_SERIALIZATION_ERROR
    return payload + "\n", None


def _write_result_file(path: Optional[Path], record: Dict[str, Any]) -> Optional[str]:
    if path is None:
        return None

    payload, serialization_error = _serialize_record(record)
    if serialization_error is not None:
        return serialization_error
    assert payload is not None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
    except (OSError, ValueError):
        return _RESULT_FILE_ERROR
    return None


def _report_result_file_failure(
    command: str,
    path: Optional[Path],
    error_code: str,
) -> int:
    if path is not None:
        failure = _record(command, "failed", error_code)
        write_error = _write_result_file(path, failure)
        if write_error is not None and write_error != error_code:
            error_code = write_error
    print(f"{command} failed: {error_code}", file=sys.stderr)
    return EXIT_RESULT_FILE


def _report_command_failure(
    command: str,
    path: Optional[Path],
    exit_code: int,
    error_code: str,
    detail: Optional[str] = None,
) -> int:
    if path is not None:
        write_error = _write_result_file(
            path,
            _record(command, "failed", error_code),
        )
        if write_error is not None:
            return _report_result_file_failure(command, path, write_error)
    suffix = f" ({detail})" if detail else ""
    print(f"{command} failed: {error_code}{suffix}", file=sys.stderr)
    return exit_code


def _input_path_error(path: Path, *, file_only: bool) -> bool:
    try:
        if not path.exists():
            return True
        if file_only and not path.is_file():
            return True
        if not file_only and not (path.is_file() or path.is_dir()):
            return True
    except (OSError, ValueError):
        return True
    return False


def _validate_config(args: argparse.Namespace) -> int:
    models_path = args.models
    result_file = args.result_file
    try:
        registry = ModelRegistry.from_yaml(models_path)
    except (ConfigurationError, OSError, ValueError, TypeError):
        return _report_command_failure(
            "validate-config",
            result_file,
            EXIT_CONFIGURATION,
            _CONFIGURATION_ERROR,
        )

    record = _record(
        "validate-config",
        "success",
        None,
        model_count=len(registry.names()),
        enabled_model_count=len(registry.enabled_names()),
        model_names=list(registry.names()),
    )
    write_error = _write_result_file(result_file, record)
    if write_error is not None:
        return _report_result_file_failure("validate-config", result_file, write_error)

    print(
        "configuration valid: "
        f"{len(registry.names())} models, "
        f"{len(registry.enabled_names())} enabled "
        f"({', '.join(registry.names())})"
    )
    return EXIT_SUCCESS


def _execute(
    command: str,
    input_path: Path,
    result_file: Optional[Path],
    handler: Optional[Callable[..., Any]],
    handler_args: Sequence[Any],
    *,
    file_only: bool,
    path_label: str,
) -> int:
    if _input_path_error(input_path, file_only=file_only):
        return _report_command_failure(
            command,
            result_file,
            EXIT_USAGE,
            _ARGUMENT_ERROR,
            f"invalid {path_label} path",
        )

    if handler is None:
        return _report_command_failure(
            command,
            result_file,
            EXIT_EXECUTION,
            _EXECUTION_NOT_CONFIGURED,
        )

    try:
        handler_result = handler(*handler_args)
    except Exception:
        return _report_command_failure(
            command,
            result_file,
            EXIT_EXECUTION,
            _EXECUTION_FAILED,
        )

    record = _record(command, "success", None, result=handler_result)
    write_error = _write_result_file(result_file, record)
    if write_error is not None:
        return _report_result_file_failure(command, result_file, write_error)

    print(f"{command} completed")
    return EXIT_SUCCESS


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    run_handler: Optional[RunHandler] = None,
    pilot_handler: Optional[PilotHandler] = None,
) -> int:
    """Run the CLI and return one of the stable T-11 exit codes."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.command == "validate-config":
        return _validate_config(args)
    if args.command == "run":
        return _execute(
            "run",
            args.fixture,
            args.result_file,
            run_handler,
            (args.fixture, args.model),
            file_only=False,
            path_label="fixture",
        )
    if args.command == "pilot":
        return _execute(
            "pilot",
            args.config,
            args.result_file,
            pilot_handler,
            (args.config,),
            file_only=True,
            path_label="config",
        )

    # ``argparse`` requires a command, so this is only a defensive boundary.
    return _report_command_failure("cli", None, EXIT_USAGE, _ARGUMENT_ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
