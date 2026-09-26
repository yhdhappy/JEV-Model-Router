"""T-11 CLI contract tests."""

import json
import inspect
import os
import subprocess
import sys
from pathlib import Path

from jev_router.cli import (
    EXIT_CONFIGURATION,
    EXIT_EXECUTION,
    EXIT_RESULT_FILE,
    EXIT_SUCCESS,
    EXIT_USAGE,
    main,
)
import jev_router.cli as cli_module


PROJECT_ROOT = Path(__file__).parents[1]


def write_valid_config(path: Path, credential_ref: str = "do-not-print") -> None:
    path.write_text(
        f"""models:
  low_model:
    provider: provider_a
    model_id: actual-low-model-id
    capability: low
    task_types:
      - coding
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: true
    credential_ref: {credential_ref}
""",
        encoding="utf-8",
    )


def test_validate_config_success_reports_safe_summary(capsys, tmp_path):
    config = tmp_path / "models.yaml"
    write_valid_config(config, credential_ref="credential-value-must-stay-private")

    code = main(["validate-config", "--models", str(config)])

    captured = capsys.readouterr()
    assert code == EXIT_SUCCESS
    assert "configuration valid" in captured.out
    assert "low_model" in captured.out
    assert "credential-value-must-stay-private" not in captured.out
    assert captured.err == ""


def test_validate_config_malformed_and_missing_configs_are_configuration_errors(
    capsys, tmp_path
):
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("models: [", encoding="utf-8")

    assert main(["validate-config", "--models", str(malformed)]) == EXIT_CONFIGURATION
    malformed_output = capsys.readouterr()
    assert "configuration_error" in malformed_output.err
    assert "models: [" not in malformed_output.err

    missing = tmp_path / "missing.yaml"
    assert main(["validate-config", "--models", str(missing)]) == EXIT_CONFIGURATION
    missing_output = capsys.readouterr()
    assert "configuration_error" in missing_output.err
    assert str(missing) not in missing_output.err


def test_run_delegates_once_and_writes_structured_result(tmp_path, capsys):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    result_file = tmp_path / "nested" / "run.json"
    calls = []

    def handler(fixture_path, model):
        calls.append((fixture_path, model))
        return {"run_id": "run-1", "status": "completed"}

    code = main(
        [
            "run",
            "--fixture",
            str(fixture),
            "--model",
            "medium_model",
            "--result-file",
            str(result_file),
        ],
        run_handler=handler,
    )

    captured = capsys.readouterr()
    assert code == EXIT_SUCCESS
    assert calls == [(fixture, "medium_model")]
    assert "run completed" in captured.out
    record = json.loads(result_file.read_text(encoding="utf-8"))
    assert record == {
        "schema_version": "0.1",
        "command": "run",
        "status": "success",
        "error_code": None,
        "result": {"run_id": "run-1", "status": "completed"},
    }
    assert result_file.parent.is_dir()
    assert len(result_file.read_text(encoding="utf-8").splitlines()) == 1


def test_pilot_delegates_once_and_writes_structured_result(tmp_path, capsys):
    config = tmp_path / "pilot.yaml"
    config.write_text("pilot: placeholder", encoding="utf-8")
    result_file = tmp_path / "pilot.json"
    calls = []

    def handler(config_path):
        calls.append(config_path)
        return {"pilot_id": "pilot-1"}

    code = main(
        ["pilot", "--config", str(config), "--result-file", str(result_file)],
        pilot_handler=handler,
    )

    captured = capsys.readouterr()
    assert code == EXIT_SUCCESS
    assert calls == [config]
    assert "pilot completed" in captured.out
    assert json.loads(result_file.read_text(encoding="utf-8"))["result"] == {
        "pilot_id": "pilot-1"
    }


def test_handler_failure_is_stable_and_does_not_echo_exception(tmp_path, capsys):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    result_file = tmp_path / "run.json"
    secret = "handler-exception-secret"

    def handler(_fixture_path, _model):
        raise RuntimeError(secret)

    code = main(
        ["run", "--fixture", str(fixture), "--result-file", str(result_file)],
        run_handler=handler,
    )

    captured = capsys.readouterr()
    assert code == EXIT_EXECUTION
    assert "execution_failed" in captured.err
    assert secret not in captured.out + captured.err
    record = json.loads(result_file.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["error_code"] == "execution_failed"
    assert secret not in result_file.read_text(encoding="utf-8")


def test_run_and_pilot_without_handlers_fail_explicitly(tmp_path, capsys):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    assert main(["run", "--fixture", str(fixture)]) == EXIT_EXECUTION
    run_output = capsys.readouterr()
    assert "execution_not_configured" in run_output.err
    assert "completed" not in run_output.out

    config = tmp_path / "pilot.yaml"
    config.write_text("pilot: placeholder", encoding="utf-8")
    assert main(["pilot", "--config", str(config)]) == EXIT_EXECUTION
    pilot_output = capsys.readouterr()
    assert "execution_not_configured" in pilot_output.err
    assert "completed" not in pilot_output.out


def test_usage_and_unknown_command_return_stable_usage_code(capsys, tmp_path):
    secret = "sk-unknown-command-secret"
    assert main([secret]) == EXIT_USAGE
    unknown_output = capsys.readouterr()
    assert "usage:" in unknown_output.err
    assert secret not in unknown_output.err

    assert main(["run"]) == EXIT_USAGE
    missing_output = capsys.readouterr()
    assert "required" in missing_output.err

    fixture = tmp_path / "fixture"
    fixture.mkdir()
    assert main(["run", "--fixture", str(fixture), "--result-file", ""]) == EXIT_USAGE
    empty_path_output = capsys.readouterr()
    assert "must not be empty" in empty_path_output.err


def test_existing_result_file_is_overwritten_with_one_command_result(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    result_file = tmp_path / "result.json"
    result_file.write_text("old data\n", encoding="utf-8")

    code = main(
        ["run", "--fixture", str(fixture), "--result-file", str(result_file)],
        run_handler=lambda _fixture_path, _model: {"fresh": True},
    )

    assert code == EXIT_SUCCESS
    text = result_file.read_text(encoding="utf-8")
    assert "old data" not in text
    assert json.loads(text)["result"] == {"fresh": True}


def test_result_file_write_failure_is_not_reported_as_success(tmp_path, capsys):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("block parent", encoding="utf-8")
    result_file = parent_file / "result.json"

    code = main(
        ["run", "--fixture", str(fixture), "--result-file", str(result_file)],
        run_handler=lambda _fixture_path, _model: {"fresh": True},
    )

    captured = capsys.readouterr()
    assert code == EXIT_RESULT_FILE
    assert "result_file_error" in captured.err
    assert "completed" not in captured.out


def test_non_json_handler_data_is_a_stable_serialization_error(tmp_path, capsys):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    result_file = tmp_path / "result.json"
    secret = "non-json-secret"

    code = main(
        ["run", "--fixture", str(fixture), "--result-file", str(result_file)],
        run_handler=lambda _fixture_path, _model: {"bad": object(), "secret": secret},
    )

    captured = capsys.readouterr()
    assert code == EXIT_RESULT_FILE
    assert "result_serialization_error" in captured.err
    assert secret not in captured.out + captured.err
    record = json.loads(result_file.read_text(encoding="utf-8"))
    assert record["error_code"] == "result_serialization_error"
    assert secret not in result_file.read_text(encoding="utf-8")


def test_control_text_cannot_forge_additional_result_records(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    result_file = tmp_path / "result.json"
    value = "first\nforged\r\nrecord\tcontrol"

    code = main(
        ["run", "--fixture", str(fixture), "--result-file", str(result_file)],
        run_handler=lambda _fixture_path, _model: {"message": value},
    )

    assert code == EXIT_SUCCESS
    lines = result_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["result"]["message"] == value


def test_obvious_input_path_errors_do_not_invoke_handlers(tmp_path, capsys):
    calls = []
    missing_fixture = tmp_path / "missing-fixture"

    code = main(
        ["run", "--fixture", str(missing_fixture)],
        run_handler=lambda *_args: calls.append(True),
    )

    captured = capsys.readouterr()
    assert code == EXIT_USAGE
    assert calls == []
    assert "fixture" in captured.err


def test_module_help_and_validate_config_invocation_work_in_subprocess(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")

    help_process = subprocess.run(
        [sys.executable, "-m", "jev_router.cli", "--help"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_process.returncode == EXIT_SUCCESS
    assert "validate-config" in help_process.stdout
    assert help_process.stderr == ""

    validate_process = subprocess.run(
        [sys.executable, "-m", "jev_router.cli", "validate-config"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert validate_process.returncode == EXIT_SUCCESS
    assert "configuration valid" in validate_process.stdout
    assert "provider-a-main" not in validate_process.stdout
    assert validate_process.stderr == ""


def test_cli_has_no_t12_runner_network_or_credential_read_surface():
    source = inspect.getsource(cli_module)

    assert "benchmark/runner.py" not in source
    assert "fixture reset" not in source.lower()
    assert "baseline" not in source.lower()
    assert "subprocess" not in source
    assert "requests" not in source
    assert "urllib" not in source
    assert "os.environ" not in source
    assert "keychain" not in source.lower()
    assert "credential_ref" not in source
