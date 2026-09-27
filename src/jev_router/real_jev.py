"""Real TypeSafe System One adapter for JEV classification.

The adapter intentionally uses the standard-library HTTP client.  It keeps the
API key process-local, sends one typed decision request per classification, and
stores only safe usage metrics from the last successful call.
"""

import json
import re
import socket
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from urllib import error as urllib_error
from urllib import request as urllib_request

from .cost import CostComponent
from .errors import ConfigurationError, SchemaValidationError
from .providers import (
    ProviderInvocationError,
    ProviderTimeoutError,
)
from .schemas import (
    ClassifierResult,
    RouteRequest,
)


SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_INPUT_PRICE_PER_MILLION = Decimal("0.042")

_TASK_TYPES = (
    "file_operation",
    "coding",
    "debugging",
    "testing",
    "review",
    "architecture",
    "research",
    "reasoning",
    "other",
)
_LEVELS = ("low", "medium", "high")
_DIFFICULTY_LEVELS = [
    "trivial, one obvious local action",
    "simple, one small change with little ambiguity",
    "straightforward, limited reasoning or verification",
    "moderate, a few related steps or files",
    "moderate, meaningful debugging or coordination",
    "substantial, several interacting decisions",
    "difficult, broad reasoning and validation",
    "very difficult, high ambiguity or system interaction",
    "complex, multiple high-impact dependencies",
    "exceptionally difficult, the hardest class of task",
]


class JEVCallMetrics:
    """Safe metrics from one successful System One call."""

    __slots__ = (
        "returned_model",
        "request_id",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "exact_cost",
    )

    def __init__(
        self,
        *,
        returned_model: str,
        request_id: Optional[str],
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        exact_cost: float,
    ) -> None:
        self.returned_model = returned_model
        self.request_id = request_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.latency_ms = latency_ms
        self.exact_cost = exact_cost

    def as_dict(self) -> Dict[str, Any]:
        return {
            "returned_model": self.returned_model,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": self.latency_ms,
            "exact_cost": self.exact_cost,
        }


class RealJEVClassifier:
    """Classify a ``RouteRequest`` through TypeSafe's real JEV API."""

    def __init__(
        self,
        api_key_file: Optional[str] = None,
        *,
        api_key_file_env: str = "JEV_API_KEY_FILE",
        endpoint: str = SYSTEM_ONE_URL,
        timeout: float = 30.0,
        urlopen: Optional[Callable[..., Any]] = None,
    ) -> None:
        key_path = api_key_file or _read_env_path(api_key_file_env)
        if not key_path:
            raise ConfigurationError("JEV API key file is not configured")
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ConfigurationError("JEV endpoint must use HTTPS")
        if timeout <= 0:
            raise ConfigurationError("JEV timeout must be positive")

        self._api_key = _read_api_key(Path(key_path))
        self._endpoint = endpoint
        self._timeout = timeout
        self._urlopen = urlopen or urllib_request.urlopen
        self._last_metrics: Optional[JEVCallMetrics] = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(endpoint={self._endpoint!r}, "
            f"timeout={self._timeout!r}, api_key=<redacted>)"
        )

    @property
    def last_call_metrics(self) -> Optional[Dict[str, Any]]:
        """Return a copy of safe metrics, never the API response or key."""

        return None if self._last_metrics is None else self._last_metrics.as_dict()

    @property
    def last_metrics(self) -> Optional[Dict[str, Any]]:
        """Compatibility alias for callers displaying the last safe metrics."""

        return self.last_call_metrics

    def classify(self, request: RouteRequest) -> ClassifierResult:
        self._last_metrics = None
        if not isinstance(request, RouteRequest):
            raise TypeError("request must be a RouteRequest")

        body = {
            "model": JEV_MODEL,
            "state": {"prompt": request.prompt, "context": list(request.context)},
            "questions": _question_schema(),
        }
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        http_request = urllib_request.Request(
            self._endpoint,
            data=encoded,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        started = time.monotonic()
        payload = self._post(http_request)
        result, metrics = _parse_response(payload, _elapsed_ms(started))
        self._last_metrics = metrics
        return result

    def resolve_cost(
        self,
        _request: RouteRequest,
        _classifier: ClassifierResult,
    ) -> CostComponent:
        """Return exact JEV input cost from the most recent successful call."""

        if self._last_metrics is None:
            raise ValueError("JEV usage is unavailable")
        return CostComponent(
            cost=self._last_metrics.exact_cost,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        )

    cost_resolver = resolve_cost

    def _post(self, http_request: urllib_request.Request) -> Mapping[str, Any]:
        try:
            response = self._urlopen(http_request, timeout=self._timeout)
            try:
                status = getattr(response, "status", 200)
                raw = response.read()
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except urllib_error.HTTPError as exc:
            if exc.code in {408, 504}:
                raise ProviderTimeoutError() from None
            raise ProviderInvocationError() from None
        except (socket.timeout, TimeoutError):
            raise ProviderTimeoutError() from None
        except (urllib_error.URLError, OSError, ConnectionError):
            raise ConnectionError("JEV network request failed") from None

        if not isinstance(status, int) or status < 200 or status >= 300:
            raise ProviderInvocationError()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
            raise SchemaValidationError(
                "JEV classifier response is not valid JSON"
            ) from None
        if not isinstance(payload, dict):
            raise SchemaValidationError(
                "JEV classifier response failed schema validation"
            )
        return payload


def _question_schema() -> Dict[str, Dict[str, Any]]:
    return {
        "task_type": {
            "type": "choice",
            "instructions": "What kind of task is this?",
            "criteria": {value: value.replace("_", " ") for value in _TASK_TYPES},
        },
        "difficulty_score": {
            "type": "score",
            "instructions": "How difficult is this task?",
            "criteria": list(_DIFFICULTY_LEVELS),
        },
        "difficulty_bucket": {
            "type": "choice",
            "instructions": "Which difficulty bucket best fits this task?",
            "criteria": {value: value for value in _LEVELS},
        },
        "required_capability": {
            "type": "choice",
            "instructions": "What minimum capability level is required?",
            "criteria": {value: value for value in _LEVELS},
        },
        "risk_level": {
            "type": "choice",
            "instructions": "What is the task risk level?",
            "criteria": {value: value for value in _LEVELS},
        },
    }


def _parse_response(
    payload: Mapping[str, Any], latency_ms: int
) -> tuple:
    answers = payload.get("answers")
    usage = payload.get("usage")
    returned_model = payload.get("model")
    if not isinstance(answers, dict) or not isinstance(usage, dict):
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        )
    if not isinstance(returned_model, str) or not returned_model:
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        )

    values = {}
    confidences = []
    for name in (
        "task_type",
        "difficulty_score",
        "difficulty_bucket",
        "required_capability",
        "risk_level",
    ):
        answer = answers.get(name)
        if not isinstance(answer, dict):
            raise SchemaValidationError(
                "JEV classifier response failed schema validation"
            )
        value_key = "score" if name == "difficulty_score" else "choice"
        if value_key not in answer:
            raise SchemaValidationError(
                "JEV classifier response failed schema validation"
            )
        values[name] = answer[value_key]
        confidence = answer.get("confidence")
        if confidence is not None:
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or confidence < 0
                or confidence > 1
            ):
                raise SchemaValidationError(
                    "JEV classifier response failed schema validation"
                )
            confidences.append(float(confidence))

    if not confidences:
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        )

    input_tokens = _usage_int(usage, "input_tokens")
    output_tokens = _usage_int(usage, "output_tokens")
    request_id = payload.get("request_id", payload.get("id"))
    if request_id is not None and not isinstance(request_id, str):
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        )

    values["difficulty_score"] = _score_to_integer(values["difficulty_score"])
    values["schema_version"] = "0.1"
    values["confidence"] = min(confidences)
    values["notes"] = None
    try:
        result = ClassifierResult.model_validate(values)
    except Exception:
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        ) from None

    exact_cost = float(
        Decimal(input_tokens) * JEV_INPUT_PRICE_PER_MILLION / Decimal("1000000")
    )
    metrics = JEVCallMetrics(
        returned_model=returned_model,
        request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        exact_cost=exact_cost,
    )
    return result, metrics


def _score_to_integer(raw: Any) -> int:
    """Map JEV's ordered raw 0..9 score to 1..10 deterministically.

    The rule is round-half-up(raw + 1), then clamp the result to 1..10.
    """

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        )
    if raw < 0 or raw > 9:
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        )
    rounded = int(
        (Decimal(str(raw)) + Decimal("1")).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return max(1, min(10, rounded))


def _usage_int(usage: Mapping[str, Any], name: str) -> int:
    value = usage.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchemaValidationError(
            "JEV classifier response failed schema validation"
        )
    return value


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _read_env_path(name: str) -> Optional[str]:
    import os

    value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _read_api_key(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        raise ConfigurationError("Unable to read JEV API key file") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigurationError("JEV API key file is not valid text") from None

    plain = _rtf_to_text(text) if "{\\rtf" in text[:32].lower() else text
    labelled = re.search(
        r"(?im)\b(?:JEV_API_KEY|TYPESAFE_API_KEY|API_KEY)\b\s*[:=]\s*"
        r"[\"']?([^\s\"'};]+)",
        plain,
    )
    if labelled:
        key = labelled.group(1).strip()
    else:
        candidates = [item for item in re.split(r"\s+", plain.strip()) if item]
        key = candidates[-1] if len(candidates) == 1 else ""
    if not key:
        raise ConfigurationError("JEV API key file does not contain a key")
    return key


def _rtf_to_text(value: str) -> str:
    """Extract visible text while skipping standard RTF metadata groups."""

    output = []
    skip_groups = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object"}
    skipped = [False]
    skip_fallback = False
    pending_ignored_destination = False
    index = 0

    while index < len(value):
        character = value[index]
        if character == "{":
            skipped.append(skipped[-1])
            index += 1
            continue
        if character == "}":
            if len(skipped) > 1:
                skipped.pop()
            index += 1
            continue
        if skip_fallback:
            if character == "\\" and index + 1 < len(value):
                index += 2
                if index < len(value) and value[index - 1] == "'":
                    index += 2
            else:
                index += 1
            skip_fallback = False
            continue
        if character != "\\":
            if not skipped[-1]:
                output.append(character)
            index += 1
            continue

        index += 1
        if index >= len(value):
            break
        symbol = value[index]
        if symbol in "\\{}":
            if not skipped[-1]:
                output.append(symbol)
            index += 1
            continue
        if symbol == "'" and index + 2 < len(value):
            try:
                decoded = bytes.fromhex(value[index + 1 : index + 3]).decode(
                    "latin-1"
                )
            except ValueError:
                decoded = ""
            if not skipped[-1]:
                output.append(decoded)
            index += 3
            continue
        if symbol == "*":
            pending_ignored_destination = True
            index += 1
            continue
        if symbol in "~_-":
            if not skipped[-1] and symbol in "~-":
                output.append(" " if symbol == "~" else "-")
            index += 1
            continue

        word_start = index
        while index < len(value) and value[index].isalpha():
            index += 1
        word = value[word_start:index]
        if index < len(value) and value[index] in "+-":
            index += 1
        while index < len(value) and value[index].isdigit():
            index += 1
        if index < len(value) and value[index] == " ":
            index += 1

        if pending_ignored_destination or word in skip_groups:
            skipped[-1] = True
            pending_ignored_destination = False
            continue
        if word == "u":
            # RTF Unicode escapes carry one fallback character, which is not
            # part of the visible text when the Unicode code point is used.
            try:
                codepoint = int(value[word_start + 1 : index].strip())
                if codepoint < 0:
                    codepoint += 65536
                if not skipped[-1]:
                    output.append(chr(codepoint))
            except (ValueError, OverflowError):
                pass
            skip_fallback = True
        elif not skipped[-1] and word in {"par", "line"}:
            output.append("\n")
        elif not skipped[-1] and word == "tab":
            output.append("\t")

    return re.sub(r"\s+", " ", "".join(output)).strip()
