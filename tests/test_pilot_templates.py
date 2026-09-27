"""Prepared Pilot fixture structure and anti-fabrication checks."""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from benchmark.runner import load_fixture
from jev_router.rules import LightweightRuleEngine
from jev_router.schemas import CapabilityLevel, RouteRequest


PROJECT_ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "benchmark" / "fixtures"
REAL_PILOT_SENTINEL = "FILL_BEFORE_REAL_PILOT"
FROZEN_README_SHA256 = "b7c956699e128337618f7ccb4060c7f8046d849889cf93a6b6a7f795c3fa2b49"
PREPARED_TASK_IDS = {
    "task_003",
    "task_004",
    "task_005",
    "task_006",
    "task_007",
    "task_008",
    "task_009",
    "task_010_fallback",
}
REAL_PROVIDER_TASK_IDS = PREPARED_TASK_IDS - {"task_010_fallback"}

TASK_SLOTS = {
    "task_001": ("light/file simple", "verify lightweight rule"),
    "task_002": ("mechanical/small change", "verify low-cost model"),
    "task_003": ("coding medium", "ordinary development"),
    "task_004": ("debug medium", "error localization"),
    "task_005": ("testing medium", "testing task"),
    "task_006": ("coding multi-step", "multi-step implementation"),
    "task_007": ("review/debug medium-high", "complex analysis"),
    "task_008": ("multi-file/context boundary", "context and capability boundary"),
    "task_009": ("architecture/reasoning high", "high-capability-model necessity"),
    "task_010_fallback": ("controlled failure injection", "fallback + budget"),
}

REQUIRED_TOP_LEVEL = {
    "task.yaml",
    "prompt.md",
    "acceptance.md",
    "initial_state",
    "expected_constraints.md",
}

EXPECTED_MODES = {
    "task_003": "manual",
    "task_004": "manual",
    "task_005": "manual",
    "task_006": "manual",
    "task_007": "manual",
    "task_008": "manual",
    "task_009": "manual",
    "task_010_fallback": "manual",
}

EXPECTED_ALLOWED_PATHS = {
    "task_003": (
        "src/jev_router/registry.py",
        "src/jev_router/errors.py",
        "tests/test_registry.py",
    ),
    "task_004": ("src/jev_router/fallback.py", "tests/test_fallback.py"),
    "task_005": (
        "src/jev_router/logging_store.py",
        "tests/test_logging_store.py",
    ),
    "task_006": ("pyproject.toml", "README.md"),
    "task_007": ("REVIEW.md",),
    "task_008": (
        "src/jev_router/logging_store.py",
        "tests/test_logging_store.py",
    ),
    "task_009": ("docs/REAL_PROVIDER_PILOT_INTEGRATION_PLAN.md",),
    "task_010_fallback": ("failure_injection_report.json",),
}

EXPECTED_INITIAL_FILES = {
    "task_003": {
        "pyproject.toml",
        "config/models.yaml",
        "src/jev_router/__init__.py",
        "src/jev_router/errors.py",
        "src/jev_router/registry.py",
        "src/jev_router/schemas.py",
        "tests/test_registry.py",
    },
    "task_004": {
        "pyproject.toml",
        "src/jev_router/__init__.py",
        "src/jev_router/cost.py",
        "src/jev_router/errors.py",
        "src/jev_router/fallback.py",
        "src/jev_router/providers.py",
        "src/jev_router/schemas.py",
        "tests/test_fallback.py",
    },
    "task_005": {
        "pyproject.toml",
        "src/jev_router/__init__.py",
        "src/jev_router/cost.py",
        "src/jev_router/errors.py",
        "src/jev_router/fallback.py",
        "src/jev_router/logging_store.py",
        "src/jev_router/policy.py",
        "src/jev_router/providers.py",
        "src/jev_router/registry.py",
        "src/jev_router/router.py",
        "src/jev_router/rules.py",
        "src/jev_router/schemas.py",
        "tests/test_logging_store.py",
    },
    "task_006": {
        "README.md",
        "benchmark/failure_injection.py",
        "benchmark/runner.py",
        "pyproject.toml",
        "tests/test_benchmark.py",
    },
    "task_007": {
        "pyproject.toml",
        "src/jev_router/__init__.py",
        "src/jev_router/cost.py",
        "src/jev_router/errors.py",
        "src/jev_router/fallback.py",
        "src/jev_router/policy.py",
        "src/jev_router/providers.py",
        "src/jev_router/registry.py",
        "src/jev_router/router.py",
        "src/jev_router/rules.py",
        "src/jev_router/schemas.py",
        "tests/test_fallback.py",
        "tests/test_providers.py",
        "tests/test_router.py",
    },
    "task_008": {
        "pyproject.toml",
        "src/jev_router/__init__.py",
        "src/jev_router/cost.py",
        "src/jev_router/errors.py",
        "src/jev_router/fallback.py",
        "src/jev_router/logging_store.py",
        "src/jev_router/policy.py",
        "src/jev_router/providers.py",
        "src/jev_router/registry.py",
        "src/jev_router/router.py",
        "src/jev_router/rules.py",
        "src/jev_router/schemas.py",
        "tests/test_logging_store.py",
        "tests/test_router.py",
    },
    "task_009": {
        "README.md",
        "config/models.yaml",
        "docs/JEV_Model_Router_产品方案_v0.1.md",
        "docs/JEV_Model_Router_产品方案_v0.2.md",
        "docs/JEV_Model_Router_产品方案_v0.3_冻结版.md",
        "docs/JEV_Model_Router_技术验证方案_v0.1.md",
        "docs/JEV_Model_Router_项目定位说明_v1.0.md",
        "src/jev_router/__init__.py",
        "src/jev_router/cli.py",
        "src/jev_router/cost.py",
        "src/jev_router/errors.py",
        "src/jev_router/fallback.py",
        "src/jev_router/logging_store.py",
        "src/jev_router/policy.py",
        "src/jev_router/providers.py",
        "src/jev_router/registry.py",
        "src/jev_router/router.py",
        "src/jev_router/rules.py",
        "src/jev_router/schemas.py",
    },
    "task_010_fallback": {
        "pyproject.toml",
        "benchmark/failure_injection.py",
        "src/jev_router/__init__.py",
        "src/jev_router/cost.py",
        "src/jev_router/errors.py",
        "src/jev_router/fallback.py",
        "src/jev_router/policy.py",
        "src/jev_router/providers.py",
        "src/jev_router/registry.py",
        "src/jev_router/router.py",
        "src/jev_router/rules.py",
        "src/jev_router/schemas.py",
    },
}

FABRICATED_RESULT = re.compile(
    r"(?im)^\s*(?:status|result|score|decision|passed|failed|go|adjust|stop)\s*[:=]"
)
SECRET_VALUE = re.compile(
    r"(?i)(?:\bauthorization\s*:\s*bearer\s+[^\s,;]+|"
    r"\bbearer[_ -]?token\s*[:=]\s*[^\s,;]+|"
    r"\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*"
    r"[\"']?[^\s,;\"']+|\bsk-[a-z0-9][a-z0-9_-]{7,})"
)
REAL_DATA_VALUE = re.compile(
    r"(?i)(?:\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"\b(?:customer|client)\s+data\s*[:=]|"
    r"\b(?:customer|client)\s+(?:name|email|id)\s*[:=]|"
    r"\bproduction\s+(?:result|config)\s*[:=])"
)


def _fixture_paths():
    return [FIXTURE_ROOT / task_id for task_id in TASK_SLOTS]


def _fixture_text(fixture: Path) -> str:
    return "\n".join(
        (fixture / name).read_text(encoding="utf-8")
        for name in ("task.yaml", "prompt.md", "acceptance.md", "expected_constraints.md")
    )


def _initial_files(fixture: Path) -> set[str]:
    return {
        path.relative_to(fixture / "initial_state").as_posix()
        for path in (fixture / "initial_state").rglob("*")
        if path.is_file()
    }


def _git_head_paths(directory: str) -> list[str]:
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", directory],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    return result.stdout.decode("utf-8").splitlines()


def test_all_ten_fixtures_are_loadable_and_have_real_snapshots():
    assert FIXTURE_ROOT.is_dir()
    assert {path.name for path in FIXTURE_ROOT.iterdir()} == set(TASK_SLOTS)

    for fixture in _fixture_paths():
        assert {path.name for path in fixture.iterdir()} == REQUIRED_TOP_LEVEL
        spec = load_fixture(fixture)
        assert spec.task_id == fixture.name
        assert spec.task_metadata["test_purpose_slot"] == TASK_SLOTS[fixture.name][0]
        assert spec.task_metadata["frozen_plan_purpose"] == TASK_SLOTS[fixture.name][1]
        assert spec.task_metadata["fixture"]["reset_before_run"] is True
        assert _initial_files(fixture) == EXPECTED_INITIAL_FILES.get(
            fixture.name, {"README.md"}
        )
        assert ".gitkeep" not in _initial_files(fixture)


@pytest.mark.parametrize("task_id", sorted(PREPARED_TASK_IDS))
def test_prepared_tasks_have_explicit_gate_mode_and_narrow_boundary(task_id):
    fixture = FIXTURE_ROOT / task_id
    spec = load_fixture(fixture)
    config = spec.task_metadata

    assert config["advisor"] == "ChatGPT Web GPT-5.6 Sol High"
    assert config["difficulty_expected_bucket"] in {"medium", "high", "controlled_mock"}
    assert spec.acceptance_mode == EXPECTED_MODES[task_id]
    assert spec.allowed_paths == EXPECTED_ALLOWED_PATHS[task_id]
    assert spec.allowed_paths_explicit is True
    assert spec.acceptance_command is None
    assert config["fixture"]["reset_before_run"] is True

    if task_id in REAL_PROVIDER_TASK_IDS:
        assert config["baseline_model"] == REAL_PILOT_SENTINEL
        assert config["budget_limit"] == REAL_PILOT_SENTINEL
        assert config["pilot_execution_gate"] == "real_provider_and_pricing_required"
    else:
        assert config["baseline_model"] == "controlled_mock_only"
        assert config["budget_limit"] == "controlled_mock_only"
        assert config["pilot_execution_gate"] == "controlled_mock_only_not_real_provider"


@pytest.mark.parametrize("task_id", sorted(PREPARED_TASK_IDS))
def test_prepared_documents_have_no_old_slots_or_fabricated_evidence(task_id):
    text = _fixture_text(FIXTURE_ROOT / task_id)

    assert "FILL_BEFORE_PILOT" not in text
    assert not FABRICATED_RESULT.search(text)
    assert not SECRET_VALUE.search(text)
    assert not REAL_DATA_VALUE.search(text)
    assert "pilot_summary" not in text.lower()
    assert "production result" not in text.lower()


def test_task_specific_prepared_contracts_are_explicit():
    task_003 = load_fixture(FIXTURE_ROOT / "task_003")
    assert "missing_model" in task_003.prompt
    assert "known-model" in task_003.prompt
    assert "tests/test_registry.py" in task_003.acceptance_text

    task_004 = load_fixture(FIXTURE_ROOT / "task_004")
    assert "called=False" in task_004.prompt
    assert "current_accumulated_cost=0.25" in task_004.prompt
    assert "called path" in task_004.acceptance_text

    task_005 = load_fixture(FIXTURE_ROOT / "task_005")
    for marker in (
        "Authorization/Bearer",
        "api_key",
        "api-key",
        "apikey",
        "access_token",
        "password",
        "secret",
        "sk-*",
        "numeric cost",
    ):
        assert marker in task_005.prompt + task_005.acceptance_text

    task_006 = load_fixture(FIXTURE_ROOT / "task_006")
    assert task_006.acceptance_mode == "manual"
    assert ".venv/bin/pytest -q tests/test_benchmark.py" in task_006.prompt
    assert "python -m pytest" in task_006.prompt

    task_007 = load_fixture(FIXTURE_ROOT / "task_007")
    assert task_007.allowed_paths == ("REVIEW.md",)
    assert "except Exception" in task_007.prompt
    assert "at least two concrete" in task_007.prompt
    assert "secret-safety" in task_007.prompt

    task_008 = load_fixture(FIXTURE_ROOT / "task_008")
    for marker in (
        'route_source="safe_default"',
        "jev_called=true",
        "fallback_used=false",
        'route_source="fallback"',
    ):
        assert marker in task_008.prompt

    task_009 = load_fixture(FIXTURE_ROOT / "task_009")
    for marker in (
        "provider-adapter boundary",
        "credential_ref",
        "real model IDs",
        "pricing source",
        "exact versus estimated usage",
        "Budget Guard",
        "error normalization",
        "placeholder-only configuration",
        "rollout",
        "rollback",
        "clearing the gate",
    ):
        assert marker in task_009.prompt

    task_010 = load_fixture(FIXTURE_ROOT / "task_010_fallback")
    assert task_010.task_metadata["failure_injection"] == {
        "helper": "benchmark.failure_injection.run_failure_injection",
        "documented_cases": ["case_a_fallback_success", "case_b_budget_block"],
    }
    assert task_010.allowed_paths == ("failure_injection_report.json",)
    assert task_010.acceptance_command is None
    assert "controlled Mock task" in (task_010.prompt + task_010.acceptance_text)
    assert "failure_injection_report.json" in task_010.acceptance_text
    assert ".venv/bin/python" in task_010.acceptance_text
    assert "call counts" in task_010.acceptance_text


def test_task_001_and_002_are_byte_identical_to_head():
    for task_id in ("task_001", "task_002"):
        prefix = f"benchmark/fixtures/{task_id}"
        expected = _git_head_paths(prefix)
        actual = sorted(
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (FIXTURE_ROOT / task_id).rglob("*")
            if path.is_file()
        )
        assert actual == expected
        for relative in expected:
            assert (PROJECT_ROOT / relative).read_bytes() == subprocess.run(
                ["git", "show", f"HEAD:{relative}"],
                cwd=PROJECT_ROOT,
                check=True,
                capture_output=True,
            ).stdout


def test_task_001_is_prepared_as_a_real_read_task_but_blocked_before_pilot():
    fixture = FIXTURE_ROOT / "task_001"
    spec = load_fixture(fixture)
    config = spec.task_metadata

    assert spec.task_id == "task_001"
    assert spec.prompt == "读取 README.md\n"
    assert config["test_purpose_slot"] == "light/file simple"
    assert config["frozen_plan_purpose"] == "verify lightweight rule"
    assert config["difficulty_expected_bucket"] == "low"
    assert config["baseline_model"] == REAL_PILOT_SENTINEL
    assert config["budget_limit"] == REAL_PILOT_SENTINEL
    assert config["pilot_execution_gate"] == "real_provider_and_pricing_required"
    assert config["fixture"]["reset_before_run"] is True
    assert config["acceptance"]["mode"] == "manual"
    assert config["acceptance"]["allowed_paths"] == []
    assert spec.acceptance_command is None
    assert spec.allowed_paths == ()
    assert spec.allowed_paths_explicit is True
    assert spec.expected_paths == ("README.md",)
    assert spec.forbidden_paths == ()

    initial_readme = (fixture / "initial_state" / "README.md").read_bytes()
    assert hashlib.sha256(initial_readme).hexdigest() == FROZEN_README_SHA256

    fixture_text = _fixture_text(fixture)
    assert "FILL_BEFORE_PILOT" not in fixture_text
    assert not FABRICATED_RESULT.search(fixture_text)
    acceptance_text = (fixture / "acceptance.md").read_text(encoding="utf-8")
    assert "light_rule" in acceptance_text
    assert "JEV" in acceptance_text
    assert "model response" in acceptance_text


def test_task_002_is_prepared_as_a_real_readme_change_but_blocked_before_pilot():
    fixture = FIXTURE_ROOT / "task_002"
    spec = load_fixture(fixture)
    config = spec.task_metadata

    assert spec.task_id == "task_002"
    assert config["name"] == "Replace the README fixture-inspection command with portable grep"
    assert config["test_purpose_slot"] == "mechanical/small change"
    assert config["frozen_plan_purpose"] == "verify low-cost model"
    assert config["difficulty_expected_bucket"] == "low"
    assert config["baseline_model"] == REAL_PILOT_SENTINEL
    assert config["budget_limit"] == REAL_PILOT_SENTINEL
    assert config["pilot_execution_gate"] == "real_provider_and_pricing_required"
    assert config["fixture"]["reset_before_run"] is True
    assert config["acceptance"]["mode"] == "automated"
    assert config["acceptance"]["allowed_paths"] == ["README.md"]
    assert spec.acceptance_mode == "automated"
    assert spec.acceptance_command is not None
    assert spec.acceptance_command[:2] == ("python3", "-c")
    assert spec.allowed_paths == ("README.md",)
    assert spec.allowed_paths_explicit is True
    assert spec.expected_paths == ("README.md",)
    assert spec.forbidden_paths == ()

    initial_readme = (fixture / "initial_state" / "README.md").read_bytes()
    assert hashlib.sha256(initial_readme).hexdigest() == FROZEN_README_SHA256

    old_command = "rg -n 'FILL_BEFORE_PILOT' benchmark/fixtures"
    new_command = "grep -R -n 'FILL_BEFORE_PILOT' benchmark/fixtures"
    assert old_command in spec.prompt
    assert new_command in spec.prompt
    assert "Modify only `README.md`" in spec.prompt
    assert "do not modify any other file" in spec.prompt

    command_text = spec.acceptance_command[2]
    assert old_command in command_text
    assert new_command in command_text
    assert "text.count(new) == 1" in command_text
    assert "old not in text" in command_text

    fixture_text = _fixture_text(fixture)
    assert "FILL_BEFORE_PILOT: replace" not in fixture_text
    assert not FABRICATED_RESULT.search(fixture_text)


def test_task_001_prompt_hits_low_lightweight_read_rule():
    result = LightweightRuleEngine().evaluate(
        RouteRequest(task_id="task_001", prompt="读取 README.md")
    )

    assert result.matched is True
    assert result.capability is CapabilityLevel.LOW
    assert result.rule_id == "simple_file_read"
