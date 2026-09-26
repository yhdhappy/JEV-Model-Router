"""Minimal, injectable JEV classification boundary for T-04."""

import json
from json import JSONDecodeError
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from .errors import ConfigurationError, SchemaValidationError
from .schemas import ClassifierResult, RouteRequest


CLASSIFIER_PROMPT_VERSION = "v0.1"
_PROMPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "prompts"
    / "jev_classifier_v0.1.txt"
)


class JEVClassifier:
    """Call an injected JEV-compatible callable and validate its JSON result.

    T-04 deliberately accepts a callable instead of defining a provider
    abstraction. Provider and fallback behavior belong to T-05 and later.
    """

    def __init__(self, call_model: Callable[[str], str]):
        if not callable(call_model):
            raise TypeError("call_model must be callable")

        try:
            self._prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(
                "Unable to read the JEV classifier prompt configuration"
            ) from None

        self._call_model = call_model

    @property
    def classifier_prompt_version(self) -> str:
        """Stable prompt version for callers and logging layers."""

        return CLASSIFIER_PROMPT_VERSION

    def build_prompt(self, request: RouteRequest) -> str:
        """Append only the current task input to the versioned prompt."""

        current_input = json.dumps(
            {"prompt": request.prompt, "context": request.context},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return f"{self._prompt_template}\n{current_input}\n"

    def classify(self, request: RouteRequest) -> ClassifierResult:
        """Return a validated ``ClassifierResult`` from one model response."""

        raw_response = self._call_model(self.build_prompt(request))
        if not isinstance(raw_response, str):
            raise SchemaValidationError(
                "JEV classifier response must be text"
            )

        try:
            payload = json.loads(raw_response)
        except (JSONDecodeError, TypeError):
            raise SchemaValidationError(
                "JEV classifier response is not valid JSON"
            ) from None

        try:
            return ClassifierResult.model_validate(payload)
        except ValidationError:
            raise SchemaValidationError(
                "JEV classifier response failed schema validation"
            ) from None
