"""Validated model configuration for the stage-1 router."""

from ast import literal_eval
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from pydantic import Field, StrictBool, StrictFloat, StrictInt, field_validator

from .errors import ConfigurationError
from .schemas import CapabilityLevel, StrictModel, TaskType


Price = Union[StrictInt, StrictFloat]


class ModelDefinition(StrictModel):
    """Configuration for one executable model."""

    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    capability: CapabilityLevel
    task_types: List[TaskType] = Field(min_length=1)
    input_cost_per_million: Price
    output_cost_per_million: Price
    enabled: StrictBool
    credential_ref: Optional[str] = Field(default=None, min_length=1)

    @field_validator("input_cost_per_million", "output_cost_per_million")
    @classmethod
    def validate_price(cls, value: Price) -> Price:
        if value < 0:
            raise ValueError("price must be greater than or equal to 0")
        return value


class ModelRegistry:
    """Load and query the configured model definitions."""

    def __init__(self, models: Mapping[str, ModelDefinition]):
        self._models = dict(models)

    @classmethod
    def from_yaml(cls, path: Union[str, Path]) -> "ModelRegistry":
        config_path = Path(path)
        try:
            raw_config = _parse_yaml(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ConfigurationError(
                "Unable to read model configuration "
                f"'{config_path}': {exc}"
            ) from exc
        except ValueError as exc:
            raise ConfigurationError(
                f"Invalid model configuration '{config_path}': {exc}"
            ) from exc

        if not isinstance(raw_config, dict) or set(raw_config) != {"models"}:
            raise ConfigurationError(
                "Model configuration must contain only a top-level 'models' mapping"
            )
        if not isinstance(raw_config["models"], dict) or not raw_config["models"]:
            raise ConfigurationError("'models' must be a non-empty mapping")

        models: Dict[str, ModelDefinition] = {}
        for name, raw_model in raw_config["models"].items():
            if not isinstance(name, str) or not name.strip():
                raise ConfigurationError("Model names must be non-empty strings")
            if not isinstance(raw_model, dict):
                raise ConfigurationError(f"Model '{name}' must be a mapping")
            try:
                models[name] = ModelDefinition.model_validate(raw_model)
            except ValueError as exc:
                raise ConfigurationError(f"Invalid model '{name}': {exc}") from exc

        return cls(models)

    @property
    def models(self) -> Mapping[str, ModelDefinition]:
        """Return an immutable view of all loaded models."""

        return MappingProxyType(self._models)

    def get(self, name: str) -> ModelDefinition:
        return self._models[name]

    def names(self) -> Tuple[str, ...]:
        return tuple(self._models)

    def enabled_names(self) -> Tuple[str, ...]:
        return tuple(name for name, model in self._models.items() if model.enabled)

    def is_enabled(self, name: str) -> bool:
        return self.get(name).enabled


def _parse_yaml(text: str) -> Any:
    """Parse the deliberately small YAML subset used by models.yaml.

    The project config contains mappings, scalar values, and lists of scalar
    task types. Keeping this parser local avoids adding a dependency for T-02;
    it is not intended to be a general-purpose YAML implementation.
    """

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
        line_number = lines[position][0]
        raise ValueError(f"unexpected indentation (line {line_number})")
    return value


def _parse_block(
    lines: Sequence[Tuple[int, int, str]], position: int, indentation: int
) -> Tuple[Any, int]:
    is_list = lines[position][1] == indentation and lines[position][2].startswith("-")
    result: Any = [] if is_list else {}

    while position < len(lines):
        line_number, current_indent, content = lines[position]
        if current_indent < indentation:
            break
        if current_indent > indentation:
            raise ValueError(f"unexpected indentation (line {line_number})")

        if is_list:
            if not content.startswith("-") or (
                len(content) > 1 and not content[1].isspace()
            ):
                raise ValueError(f"expected list item (line {line_number})")
            item = content[1:].strip()
            if not item:
                raise ValueError(f"list item must have a value (line {line_number})")
            result.append(_parse_scalar(item))
            position += 1
            continue

        if content.startswith("-"):
            raise ValueError(f"unexpected list item (line {line_number})")
        if ":" not in content:
            raise ValueError(f"expected mapping entry (line {line_number})")
        key, raw_value = content.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"mapping key cannot be empty (line {line_number})")
        if key in result:
            raise ValueError(f"duplicate mapping key '{key}' (line {line_number})")

        raw_value = raw_value.strip()
        position += 1
        if raw_value:
            result[key] = _parse_scalar(raw_value)
            continue
        if position >= len(lines) or lines[position][1] <= indentation:
            raise ValueError(f"mapping key '{key}' has no value (line {line_number})")
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
        return [_parse_scalar(item.strip()) for item in value[1:-1].split(",") if item.strip()]
    try:
        return literal_eval(value)
    except (ValueError, SyntaxError):
        return value


def _strip_comment(value: str) -> str:
    quote = None
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            quote = None if quote == character else character if quote is None else quote
        elif character == "#" and quote is None:
            return value[:index]
    return value
