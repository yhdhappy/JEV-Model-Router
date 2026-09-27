"""T-14 Pilot fixture structure and anti-fabrication checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from benchmark.runner import load_fixture
from jev_router.rules import LightweightRuleEngine
from jev_router.schemas import CapabilityLevel, RouteRequest


PROJECT_ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "benchmark" / "fixtures"
PLACEHOLDER = "FILL_BEFORE_PILOT"
REAL_PILOT_SENTINEL = "FILL_BEFORE_REAL_PILOT"
PREPARED_TASK_IDS = {"task_001", "task_002"}

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
FABRICATED_RESULT = re.compile(
    r"(?im)^\s*(?:status|result|score|decision|passed|failed|go|adjust|stop)\s*[:=]"
)
SECRET_OR_REAL_DATA = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{8,}|\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|"
    r"(?:customer|client)\s+data|production\s+(?:result|config))"
)


def _fixture_paths():
    return [FIXTURE_ROOT / task_id for task_id in TASK_SLOTS]


def _template_task_ids():
    return [task_id for task_id in TASK_SLOTS if task_id not in PREPARED_TASK_IDS]


def _template_text(fixture: Path) -> str:
    return "\n".join(
        (fixture / name).read_text(encoding="utf-8")
        for name in ("task.yaml", "prompt.md", "acceptance.md", "expected_constraints.md")
    )


def _assert_placeholder_document(text: str) -> None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*]\s*(?:\[[ xX]\]\s*)?", "", line)
        assert line.startswith(PLACEHOLDER)


def test_all_ten_fixtures_have_exact_documented_structure():
    assert FIXTURE_ROOT.is_dir()
    assert {path.name for path in FIXTURE_ROOT.iterdir()} == set(TASK_SLOTS)

    for fixture in _fixture_paths():
        assert {path.name for path in fixture.iterdir()} == REQUIRED_TOP_LEVEL
        assert (fixture / "initial_state").is_dir()
        initial_names = [path.name for path in (fixture / "initial_state").iterdir()]
        if fixture.name in PREPARED_TASK_IDS:
            assert initial_names == ["README.md"]
            assert not (fixture / "initial_state" / "README.md").is_symlink()
        else:
            assert initial_names == [".gitkeep"]
            assert not (fixture / "initial_state" / ".gitkeep").is_symlink()


@pytest.mark.parametrize("task_id", _template_task_ids())
def test_remaining_tasks_load_with_twelve_fixture_contract_and_remain_templates(task_id):
    fixture = FIXTURE_ROOT / task_id
    spec = load_fixture(fixture)
    config = spec.task_metadata

    assert spec.task_id == task_id
    assert config["test_purpose_slot"] == TASK_SLOTS[task_id][0]
    assert config["frozen_plan_purpose"] == TASK_SLOTS[task_id][1]
    assert config["difficulty_expected_bucket"] == PLACEHOLDER
    assert config["baseline_model"] == PLACEHOLDER
    assert config["budget_limit"] == PLACEHOLDER
    assert config["acceptance"]["mode"] == "manual"
    assert spec.acceptance_command is None

    for name in ("prompt.md", "acceptance.md", "expected_constraints.md"):
        text = (fixture / name).read_text(encoding="utf-8")
        assert PLACEHOLDER in text
        assert not FABRICATED_RESULT.search(text)
        _assert_placeholder_document(text)


@pytest.mark.parametrize("task_id", _template_task_ids())
def test_templates_contain_no_obvious_business_data_or_pilot_results(task_id):
    fixture = FIXTURE_ROOT / task_id
    text = _template_text(fixture)

    assert not SECRET_OR_REAL_DATA.search(text)
    assert "pilot_summary" not in text.lower()
    assert "benchmark result" not in text.lower()
    assert "real provider" not in text.lower()


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
    current_readme = (PROJECT_ROOT / "README.md").read_bytes()
    assert initial_readme == current_readme

    fixture_text = _template_text(fixture)
    assert PLACEHOLDER not in fixture_text
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
    current_readme = (PROJECT_ROOT / "README.md").read_bytes()
    assert initial_readme == current_readme

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

    fixture_text = _template_text(fixture)
    assert "FILL_BEFORE_PILOT: replace" not in fixture_text
    assert not FABRICATED_RESULT.search(fixture_text)


def test_task_001_prompt_hits_low_lightweight_read_rule():
    result = LightweightRuleEngine().evaluate(
        RouteRequest(task_id="task_001", prompt="读取 README.md")
    )

    assert result.matched is True
    assert result.capability is CapabilityLevel.LOW
    assert result.rule_id == "simple_file_read"


def test_task_010_declares_only_the_reusable_t13_seam():
    config = load_fixture(FIXTURE_ROOT / "task_010_fallback").task_metadata
    failure_injection = config["failure_injection"]

    assert failure_injection == {
        "helper": "benchmark.failure_injection.run_failure_injection",
        "documented_cases": ["case_a_fallback_success", "case_b_budget_block"],
    }
    assert "credential" not in _template_text(FIXTURE_ROOT / "task_010_fallback").lower()
    assert "provider_config" not in _template_text(FIXTURE_ROOT / "task_010_fallback").lower()
