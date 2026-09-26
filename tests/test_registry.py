"""T-02 model registry tests."""

from pathlib import Path

import pytest

from jev_router.errors import ConfigurationError
from jev_router.registry import ModelRegistry
from jev_router.schemas import CapabilityLevel, TaskType


PROJECT_ROOT = Path(__file__).parents[1]
MODELS_CONFIG = PROJECT_ROOT / "config" / "models.yaml"


def test_loads_the_three_capability_tiers_from_config():
    registry = ModelRegistry.from_yaml(MODELS_CONFIG)

    assert set(registry.names()) == {"low_model", "medium_model", "high_model"}
    assert registry.get("low_model").capability is CapabilityLevel.LOW
    assert registry.get("medium_model").capability is CapabilityLevel.MEDIUM
    assert registry.get("high_model").capability is CapabilityLevel.HIGH
    assert TaskType.ARCHITECTURE in registry.get("high_model").task_types


def test_rejects_negative_model_price(tmp_path):
    config = tmp_path / "models.yaml"
    config.write_text(
        """models:
  low_model:
    provider: provider_a
    model_id: low
    capability: low
    task_types:
      - coding
    input_cost_per_million: -1
    output_cost_per_million: 0
    enabled: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="input_cost_per_million"):
        ModelRegistry.from_yaml(config)


def test_rejects_unknown_model_capability(tmp_path):
    config = tmp_path / "models.yaml"
    config.write_text(
        """models:
  low_model:
    provider: provider_a
    model_id: low
    capability: extreme
    task_types:
      - coding
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="capability"):
        ModelRegistry.from_yaml(config)


def test_rejects_unknown_task_type(tmp_path):
    config = tmp_path / "models.yaml"
    config.write_text(
        """models:
  low_model:
    provider: provider_a
    model_id: low
    capability: low
    task_types:
      - unsupported_task
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: true
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="task_types"):
        ModelRegistry.from_yaml(config)


def test_rejects_non_boolean_enabled_value(tmp_path):
    config = tmp_path / "models.yaml"
    config.write_text(
        """models:
  low_model:
    provider: provider_a
    model_id: low
    capability: low
    task_types:
      - coding
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: maybe
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="enabled"):
        ModelRegistry.from_yaml(config)


def test_disabled_model_is_loaded_and_identified(tmp_path):
    config = tmp_path / "models.yaml"
    config.write_text(
        """models:
  paused_model:
    provider: provider_a
    model_id: paused
    capability: low
    task_types:
      - coding
    input_cost_per_million: 0
    output_cost_per_million: 0
    enabled: false
""",
        encoding="utf-8",
    )

    registry = ModelRegistry.from_yaml(config)

    assert registry.get("paused_model").enabled is False
    assert registry.is_enabled("paused_model") is False
    assert registry.enabled_names() == ()
