"""Offline tests for the declarative official Pilot config freeze."""

from pathlib import Path

import pytest

from benchmark.pilot_config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_MODEL_REGISTRY_PATH,
    PilotConfigError,
    load_pilot_config,
)
from jev_router.cost import CostComponent
from jev_router.providers import MockProvider, ModelResponse
from jev_router.registry import ModelRegistry
from jev_router.router import Router
from jev_router.schemas import ClassifierResult, RouteRequest


ROOT = Path(__file__).parents[1]
OFFICIAL_RESULTS = ROOT / "benchmark" / "results" / "pilot_runs.jsonl"


def _config_text() -> str:
    return DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "pilot_config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_frozen_config_loads_against_enabled_real_pilot_models_without_side_effects():
    before = OFFICIAL_RESULTS.read_bytes() if OFFICIAL_RESULTS.exists() else None

    config = load_pilot_config()

    assert config.total_task_slots == 10
    assert config.real_task_ids == tuple(f"task_{number:03d}" for number in range(1, 10))
    assert config.controlled_failure_task_id == "task_010_fallback"
    assert config.baseline_model == "high_model"
    assert config.budget_limit_per_real_task == 1.25
    assert dict(config.estimated_max_costs) == {
        "low_model": 0.10,
        "medium_model": 0.25,
        "high_model": 1.00,
    }
    assert config.max_extra_runs_per_task == 2
    assert config.jev_confidence_threshold == 0.70
    assert config.difficulty_boundary_scores == (3, 4, 7, 8)
    assert config.audit_timing == "after_execution"
    assert config.audit_cost_included_in_production_route_cost is False
    assert config.outcome is None
    assert config.outputs.official_results_path == "benchmark/results/pilot_runs.jsonl"
    assert not OFFICIAL_RESULTS.exists() or OFFICIAL_RESULTS.read_bytes() == before


def test_frozen_budget_has_positive_classifier_headroom_for_high_model():
    config = load_pilot_config()

    assert config.budget_limit_per_real_task > max(config.estimated_max_costs.values())


def test_loader_rejects_budget_equal_to_enabled_model_estimate(tmp_path):
    path = _write_config(
        tmp_path,
        _config_text().replace("budget_limit_per_real_task: 1.25", "budget_limit_per_real_task: 1.00"),
    )

    with pytest.raises(
        PilotConfigError,
        match="budget limit must be strictly greater than every enabled model estimate",
    ):
        load_pilot_config(path)


def test_positive_classifier_cost_does_not_budget_block_high_model():
    provider = MockProvider(
        [
            ModelResponse(
                text="high model result",
                input_tokens=10,
                output_tokens=5,
                provider_request_id="offline-high-model",
            )
        ]
    )
    router = Router(
        ModelRegistry.from_yaml(DEFAULT_MODEL_REGISTRY_PATH),
        {"high_model": provider},
        lambda request: ClassifierResult(
            task_type="coding",
            difficulty_score=9,
            difficulty_bucket="high",
            required_capability="high",
            confidence=0.95,
            risk_level="high",
        ),
        safe_default_model="high_model",
        estimated_max_costs={"high_model": 1.00},
        classifier_cost_resolver=lambda request, result: CostComponent(
            cost=0.01,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        ),
    )

    result = router.route(
        RouteRequest(
            task_id="pilot-budget-headroom",
            prompt="debug the failing login flow",
            budget_limit=1.25,
        )
    )

    assert result.status == "success"
    assert result.selected_model == "high_model"
    assert result.errors == []
    assert provider.call_count == 1


def test_fixture_placeholders_are_not_needed_or_modified_by_config_loading():
    fixture_files = sorted((ROOT / "benchmark" / "fixtures").glob("*/task.yaml"))
    before = {path: path.read_bytes() for path in fixture_files}

    config = load_pilot_config()

    assert config.baseline_model != "FILL_BEFORE_REAL_PILOT"
    assert {path: path.read_bytes() for path in fixture_files} == before
    assert all(b"FILL_BEFORE_REAL_PILOT" in data for data in before.values() if b"task_010_fallback" not in data)


def test_config_has_no_official_result_write_authorization():
    before = OFFICIAL_RESULTS.read_bytes() if OFFICIAL_RESULTS.exists() else None
    assert "allow_official" not in _config_text()
    assert "JEV_API_KEY" not in _config_text()
    load_pilot_config()
    assert not OFFICIAL_RESULTS.exists() or OFFICIAL_RESULTS.read_bytes() == before


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        ("schema_version: \"0.1\"", "schema_version: \"0.2\""),
        ("  max_extra_runs_per_task: 2", "  max_extra_runs_per_task: 1"),
        ("  jev_confidence_threshold: 0.70", "  jev_confidence_threshold: 1.5"),
        ("    - 8\n  triggers:", "    - 9\n  triggers:"),
        ("  summary_json_path: benchmark/results/pilot_summary.json", "  summary_json_path: /tmp/pilot_summary.json"),
        ("  outcome: null", "  outcome: go"),
    ],
)
def test_loader_rejects_malformed_or_unsafe_values(tmp_path, needle, replacement):
    path = _write_config(tmp_path, _config_text().replace(needle, replacement))

    with pytest.raises(PilotConfigError):
        load_pilot_config(path)


def test_loader_rejects_unknown_top_level_key(tmp_path):
    path = _write_config(tmp_path, _config_text() + "\nunknown_top_level: true\n")

    with pytest.raises(PilotConfigError, match="unknown keys"):
        load_pilot_config(path)


def test_loader_rejects_duplicate_task_ids(tmp_path):
    path = _write_config(tmp_path, _config_text().replace("    - task_009\n", "    - task_008\n"))

    with pytest.raises(PilotConfigError):
        load_pilot_config(path)


def test_loader_requires_a_max_cost_for_every_enabled_model(tmp_path):
    text = _config_text().replace("    high_model: 1.00\n", "")
    path = _write_config(tmp_path, text)

    with pytest.raises(PilotConfigError, match="missing an enabled model"):
        load_pilot_config(path)


def test_loader_rejects_disabled_baseline(tmp_path):
    registry_text = DEFAULT_MODEL_REGISTRY_PATH.read_text(encoding="utf-8")
    registry_path = tmp_path / "models.real-pilot.yaml"
    registry_path.write_text(registry_text.rsplit("    enabled: true", 1)[0] + "    enabled: false\n", encoding="utf-8")

    with pytest.raises(PilotConfigError, match="baseline_model"):
        load_pilot_config(model_registry_path=registry_path)


def test_loader_rejects_secret_home_path_or_raw_provider_text(tmp_path):
    path = _write_config(tmp_path, _config_text() + "\n# /Users/example/secret_path\n")

    with pytest.raises(PilotConfigError, match="secret"):
        load_pilot_config(path)


def test_output_paths_are_relative_distinct_and_inside_results():
    config = load_pilot_config()
    values = (
        config.outputs.official_results_path,
        config.outputs.summary_json_path,
        config.outputs.summary_markdown_path,
        config.outputs.artifacts_dir,
    )
    assert len(values) == len(set(values))
    assert all(value.startswith("benchmark/results/") for value in values)
    assert all(not Path(value).is_absolute() for value in values)
