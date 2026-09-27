"""T-14 Pilot template structure and anti-fabrication checks."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from benchmark.runner import load_fixture


PROJECT_ROOT = Path(__file__).parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "benchmark" / "fixtures"
PLACEHOLDER = "FILL_BEFORE_PILOT"

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


def test_all_ten_templates_have_exact_documented_structure():
    assert FIXTURE_ROOT.is_dir()
    assert {path.name for path in FIXTURE_ROOT.iterdir()} == set(TASK_SLOTS)

    for fixture in _fixture_paths():
        assert {path.name for path in fixture.iterdir()} == REQUIRED_TOP_LEVEL
        assert (fixture / "initial_state").is_dir()
        assert [path.name for path in (fixture / "initial_state").iterdir()] == [".gitkeep"]
        assert not (fixture / "initial_state" / ".gitkeep").is_symlink()


@pytest.mark.parametrize("task_id", TASK_SLOTS)
def test_template_loads_with_twelve_fixture_contract_and_remains_unpopulated(task_id):
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


@pytest.mark.parametrize("task_id", TASK_SLOTS)
def test_templates_contain_no_obvious_business_data_or_pilot_results(task_id):
    fixture = FIXTURE_ROOT / task_id
    text = _template_text(fixture)

    assert not SECRET_OR_REAL_DATA.search(text)
    assert "pilot_summary" not in text.lower()
    assert "benchmark result" not in text.lower()
    assert "real provider" not in text.lower()


def test_task_010_declares_only_the_reusable_t13_seam():
    config = load_fixture(FIXTURE_ROOT / "task_010_fallback").task_metadata
    failure_injection = config["failure_injection"]

    assert failure_injection == {
        "helper": "benchmark.failure_injection.run_failure_injection",
        "documented_cases": ["case_a_fallback_success", "case_b_budget_block"],
    }
    assert "credential" not in _template_text(FIXTURE_ROOT / "task_010_fallback").lower()
    assert "provider_config" not in _template_text(FIXTURE_ROOT / "task_010_fallback").lower()
