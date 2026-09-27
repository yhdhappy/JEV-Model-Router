"""T-12 benchmark fixture and harness tests."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark.runner import (
    FixtureValidationError,
    load_fixture,
    run_fixture,
    run_pair,
)


def make_fixture(
    root: Path,
    *,
    mode: str = "automated",
    command=None,
    allowed_paths=None,
    expected_paths=None,
    forbidden_paths=None,
    constraints_text: str = "",
    initial_text: str = "original\n",
) -> Path:
    fixture = root / "fixture"
    initial = fixture / "initial_state"
    initial.mkdir(parents=True)
    (fixture / "task.yaml").write_text(
        "id: task-test\n"
        "name: isolated fixture\n"
        "baseline_model: baseline-test\n"
        "budget_limit: 0.50\n"
        "fixture:\n"
        "  reset_before_run: true\n"
        "acceptance:\n"
        f"  mode: {mode}\n"
        + (f"  command: {json.dumps(command)}\n" if command is not None else "")
        + (
            "  allowed_paths: []\n"
            if allowed_paths == []
            else (
                "  allowed_paths:\n"
                + "".join(f"    - {json.dumps(path)}\n" for path in allowed_paths)
            )
            if allowed_paths is not None
            else ""
        )
        + (
            "  expected_paths:\n"
            + "".join(f"    - {json.dumps(path)}\n" for path in expected_paths)
            if expected_paths is not None
            else ""
        )
        + (
            "  forbidden_paths:\n"
            + "".join(f"    - {json.dumps(path)}\n" for path in forbidden_paths)
            if forbidden_paths is not None
            else ""
        ),
        encoding="utf-8",
    )
    (fixture / "prompt.md").write_text("Do the isolated task.\n", encoding="utf-8")
    (fixture / "acceptance.md").write_text(
        "Human criteria: inspect the resulting file.\n", encoding="utf-8"
    )
    (fixture / "expected_constraints.md").write_text(
        constraints_text, encoding="utf-8"
    )
    (initial / "state.txt").write_text(initial_text, encoding="utf-8")
    return fixture


def test_valid_fixture_loads_documented_contract(tmp_path):
    fixture = make_fixture(
        tmp_path,
        command=[sys.executable, "-c", "print('ok')"],
        allowed_paths=["output.txt"],
    )

    spec = load_fixture(fixture)

    assert spec.task_id == "task-test"
    assert spec.baseline_model == "baseline-test"
    assert spec.acceptance_mode == "automated"
    assert spec.acceptance_command == (sys.executable, "-c", "print('ok')")
    assert spec.allowed_paths == ("output.txt",)


@pytest.mark.parametrize(
    "missing",
    ["task.yaml", "prompt.md", "acceptance.md", "initial_state", "expected_constraints.md"],
)
def test_missing_required_fixture_parts_have_stable_validation_error(tmp_path, missing):
    fixture = make_fixture(tmp_path)
    target = fixture / missing
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    with pytest.raises(FixtureValidationError) as exc_info:
        load_fixture(fixture)

    assert exc_info.value.code == "fixture_validation_error"
    assert str(exc_info.value).startswith("fixture_validation_error:")


def test_malformed_task_yaml_has_stable_validation_error(tmp_path):
    fixture = make_fixture(tmp_path)
    (fixture / "task.yaml").write_text("acceptance: [\n", encoding="utf-8")

    with pytest.raises(FixtureValidationError, match="fixture_validation_error"):
        load_fixture(fixture)


def test_two_runs_start_from_pristine_isolated_snapshots(tmp_path):
    fixture = make_fixture(
        tmp_path,
        mode="manual",
        constraints_text="expected_paths:\n  - output.txt\n",
    )
    seen = []

    def executor(workspace, metadata, prompt):
        seen.append((workspace, (workspace / "state.txt").read_text(), metadata["baseline_model"], prompt))
        (workspace / "state.txt").write_text("mutated\n", encoding="utf-8")
        (workspace / "output.txt").write_text("done\n", encoding="utf-8")
        return {"status": "completed", "secret": "sk-do-not-return"}

    first = run_fixture(fixture, "baseline", baseline_executor=executor)
    second = run_fixture(fixture, "baseline", baseline_executor=executor)

    assert first["status"] == "pending_manual"
    assert second["status"] == "pending_manual"
    assert [item[1] for item in seen] == ["original\n", "original\n"]
    assert seen[0][0] != seen[1][0]
    assert first["workspace_initial_hash"] == second["workspace_initial_hash"]
    assert (fixture / "initial_state" / "state.txt").read_text() == "original\n"
    assert "sk-do-not-return" not in json.dumps(first)
    assert not seen[0][0].exists()
    assert not seen[1][0].exists()


def test_baseline_and_router_each_run_once_in_fresh_workspaces(tmp_path):
    fixture = make_fixture(tmp_path, mode="manual")
    calls = []

    def baseline(workspace, metadata, prompt):
        calls.append(("baseline", workspace))
        (workspace / "baseline.txt").write_text("b", encoding="utf-8")
        return {"status": "baseline-ok"}

    def router(workspace, metadata, prompt):
        calls.append(("router", workspace))
        (workspace / "router.txt").write_text("r", encoding="utf-8")
        return {"status": "router-ok"}

    pair = run_pair(fixture, baseline, router)

    assert [kind for kind, _ in calls] == ["baseline", "router"]
    assert calls[0][1] != calls[1][1]
    assert pair["task_id"] == "task-test"
    assert pair["baseline"]["mode"] == "baseline"
    assert pair["router"]["mode"] == "router"
    assert pair["baseline"]["status"] == "pending_manual"
    assert pair["router"]["status"] == "pending_manual"
    assert json.dumps(pair, sort_keys=True)


def test_automated_acceptance_success_and_failure(tmp_path):
    success_fixture = make_fixture(
        tmp_path / "success",
        command=[sys.executable, "-c", "print('acceptance-ok')"],
        allowed_paths=[],
    )
    success = run_fixture(
        success_fixture,
        "baseline",
        baseline_executor=lambda workspace, metadata, prompt: None,
    )
    assert success["status"] == "passed"
    assert success["acceptance"]["status"] == "passed"
    assert success["acceptance"]["exit_code"] == 0
    assert "acceptance-ok" in success["acceptance"]["stdout"]

    failure_fixture = make_fixture(
        tmp_path / "failure",
        command=[sys.executable, "-c", "import sys; print('bad'); sys.exit(3)"],
        allowed_paths=[],
    )
    failure = run_fixture(
        failure_fixture,
        "baseline",
        baseline_executor=lambda workspace, metadata, prompt: None,
    )
    assert failure["status"] == "failed"
    assert failure["acceptance"]["status"] == "failed"
    assert failure["acceptance"]["exit_code"] == 3


def test_acceptance_command_is_shlex_split_and_never_shell_interpreted(tmp_path):
    fixture = make_fixture(
        tmp_path,
        command="python -c \"import sys; print('shell-safe'); sys.exit(0)\"",
        allowed_paths=[],
    )
    observed = {}

    def command_runner(argv, **kwargs):
        observed["argv"] = tuple(argv)
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    result = run_fixture(
        fixture,
        "baseline",
        baseline_executor=lambda workspace, metadata, prompt: None,
        command_runner=command_runner,
    )

    assert result["status"] == "passed"
    assert observed["shell"] is False
    assert observed["cwd"].name == "workspace"
    assert observed["argv"] == ("python", "-c", "import sys; print('shell-safe'); sys.exit(0)")


def test_acceptance_timeout_is_bounded_stable_and_cleans_workspace(tmp_path):
    fixture = make_fixture(
        tmp_path,
        command=[sys.executable, "-c", "print('never completes')"],
        allowed_paths=[],
    )
    observed = {}
    workspaces = []

    def executor(workspace, metadata, prompt):
        workspaces.append(workspace)

    def command_runner(argv, **kwargs):
        observed.update(kwargs)
        assert kwargs["timeout"] > 0
        raise subprocess.TimeoutExpired(
            cmd=["secret-timeout-command"],
            timeout=kwargs["timeout"],
            output="secret-timeout-output",
            stderr="secret-timeout-error",
        )

    result = run_fixture(
        fixture,
        "baseline",
        baseline_executor=executor,
        command_runner=command_runner,
    )

    assert result["status"] == "failed"
    assert result["acceptance"] == {
        "mode": "automated",
        "status": "failed",
        "passed": False,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "error_code": "acceptance_command_timeout",
        "checks": [{"name": "command", "passed": False, "exit_code": None}],
    }
    assert observed["shell"] is False
    assert result["source_unchanged"] is True
    assert result["workspace_removed"] is True
    assert not workspaces[0].exists()
    output = json.dumps(result, sort_keys=True)
    assert "secret-timeout-command" not in output
    assert "secret-timeout-output" not in output
    assert "secret-timeout-error" not in output


@pytest.mark.parametrize("bad_path", ["../escape", "/absolute", "C:\\absolute", "a/../../b"])
def test_path_traversal_and_absolute_constraints_are_rejected(tmp_path, bad_path):
    fixture = make_fixture(tmp_path, mode="manual", allowed_paths=[bad_path])

    with pytest.raises(FixtureValidationError, match="path"):
        load_fixture(fixture)


def test_allowed_paths_and_explicit_expected_forbidden_constraints(tmp_path):
    fixture = make_fixture(
        tmp_path,
        mode="manual",
        allowed_paths=["allowed/", "expected.txt"],
        constraints_text="Expected paths:\n- expected.txt\nForbidden paths:\n- forbidden.txt\n",
    )

    def allowed_executor(workspace, metadata, prompt):
        (workspace / "allowed").mkdir()
        (workspace / "allowed" / "changed.txt").write_text("x", encoding="utf-8")
        (workspace / "expected.txt").write_text("x", encoding="utf-8")

    passed = run_fixture(fixture, "baseline", baseline_executor=allowed_executor)
    assert passed["status"] == "pending_manual"
    assert passed["acceptance"]["checks"][-1]["passed"] is True

    bad_fixture = make_fixture(tmp_path / "bad", mode="manual", allowed_paths=["allowed/"])

    def bad_executor(workspace, metadata, prompt):
        (workspace / "outside.txt").write_text("x", encoding="utf-8")

    failed = run_fixture(bad_fixture, "baseline", baseline_executor=bad_executor)
    assert failed["status"] == "failed"
    assert any(check["name"] == "allowed_paths" and not check["passed"] for check in failed["acceptance"]["checks"])


def test_forbidden_constraint_fails_even_when_executor_succeeds(tmp_path):
    fixture = make_fixture(
        tmp_path,
        mode="manual",
        constraints_text="forbidden_paths:\n  - secret.txt\n",
    )

    result = run_fixture(
        fixture,
        "baseline",
        baseline_executor=lambda workspace, metadata, prompt: (workspace / "secret.txt").write_text("x"),
    )

    assert result["status"] == "failed"
    assert any(check["name"] == "forbidden_paths" and not check["passed"] for check in result["acceptance"]["checks"])


def test_manual_acceptance_is_pending_and_has_no_fabricated_score(tmp_path):
    fixture = make_fixture(tmp_path, mode="manual")

    result = run_fixture(
        fixture,
        "baseline",
        baseline_executor=lambda workspace, metadata, prompt: None,
    )

    assert result["status"] == "pending_manual"
    assert result["acceptance"]["mode"] == "manual"
    assert result["acceptance"]["status"] == "pending_manual"
    assert result["acceptance"]["passed"] is None
    assert "score" not in result["acceptance"]


def test_executor_exception_is_stable_and_workspace_is_cleaned(tmp_path):
    fixture = make_fixture(tmp_path, mode="manual")
    observed = []

    def executor(workspace, metadata, prompt):
        observed.append(workspace)
        (workspace / "partial.txt").write_text("partial", encoding="utf-8")
        raise RuntimeError("secret executor details")

    result = run_fixture(fixture, "baseline", baseline_executor=executor)

    assert result["status"] == "failed"
    assert result["errors"] == [{"code": "executor_failed", "message": "Executor failed"}]
    assert result["acceptance"]["status"] == "not_run"
    assert result["workspace_removed"] is True
    assert not observed[0].exists()
    assert "secret executor details" not in json.dumps(result)
    assert (fixture / "initial_state" / "state.txt").read_text() == "original\n"


def test_acceptance_runner_failure_is_stable_and_source_is_unchanged(tmp_path):
    fixture = make_fixture(tmp_path, command=["not-a-real-command"], allowed_paths=[])
    before = (fixture / "initial_state" / "state.txt").read_bytes()

    def command_runner(argv, **kwargs):
        raise RuntimeError("acceptance secret")

    result = run_fixture(
        fixture,
        "baseline",
        baseline_executor=lambda workspace, metadata, prompt: None,
        command_runner=command_runner,
    )

    assert result["status"] == "failed"
    assert result["acceptance"]["status"] == "failed"
    assert result["acceptance"]["error_code"] == "acceptance_command_failed"
    assert (fixture / "initial_state" / "state.txt").read_bytes() == before
    assert result["source_unchanged"] is True
    assert "acceptance secret" not in json.dumps(result)


def test_run_pair_summary_is_deterministic_for_identical_executors(tmp_path):
    fixture = make_fixture(tmp_path, mode="manual")

    def executor(workspace, metadata, prompt):
        (workspace / "result.txt").write_text("same", encoding="utf-8")
        return {"status": "completed"}

    first = run_pair(fixture, executor, executor)
    second = run_pair(fixture, executor, executor)

    assert first == second
    assert set(first) == {"task_id", "baseline", "router"}
    assert first["baseline"]["executor"]["status"] == "completed"


def test_runner_has_no_network_provider_surface():
    project_root = Path(__file__).parents[1]
    source = (project_root / "benchmark" / "runner.py").read_text(encoding="utf-8")
    assert "requests" not in source
    assert "urllib" not in source
    assert "Provider" not in source
