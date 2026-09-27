"""Authenticated local OpenCode Go execution provider.

Authentication is deliberately owned by the local ``opencode`` CLI.  This
provider passes a list of argv strings to subprocess and never reads or copies
CLI credentials.
"""

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .providers import (
    ModelRequest,
    ModelResponse,
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


class OpenCodeCallMetrics:
    """Safe usage and cost metrics from the last OpenCode invocation."""

    def __init__(
        self,
        *,
        model_id: str,
        request_id: Optional[str],
        input_tokens: int,
        output_tokens: int,
        reasoning_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        provider_reported_cost: Optional[float],
        latency_ms: int,
    ) -> None:
        self.model_id = model_id
        self.request_id = request_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.reasoning_tokens = reasoning_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.provider_reported_cost = provider_reported_cost
        self.latency_ms = latency_ms

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "request_id": self.request_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "provider_reported_cost": self.provider_reported_cost,
            "latency_ms": self.latency_ms,
        }


class OpenCodeGoProvider:
    """Invoke one model through the locally authenticated OpenCode CLI."""

    def __init__(
        self,
        *,
        timeout: float = 120.0,
        executable: str = "opencode",
        run: Optional[Callable[..., Any]] = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not isinstance(executable, str) or not executable:
            raise ValueError("executable must be a non-empty string")
        self._timeout = timeout
        self._executable = executable
        self._run = run or subprocess.run
        self._last_metrics: Optional[OpenCodeCallMetrics] = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(executable={self._executable!r}, "
            f"timeout={self._timeout!r})"
        )

    @property
    def last_call_metrics(self) -> Optional[Dict[str, Any]]:
        return None if self._last_metrics is None else self._last_metrics.as_dict()

    @property
    def last_metrics(self) -> Optional[Dict[str, Any]]:
        return self.last_call_metrics

    def invoke(self, request: ModelRequest) -> ModelResponse:
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be a ModelRequest")

        cwd, temporary = _working_directory(request)
        argv = [
            self._executable,
            "run",
            "--dir",
            str(cwd),
            "-m",
            request.model_id,
            "--format",
            "json",
            "--pure",
        ]
        if request.metadata.get("auto") is True:
            argv.append("--auto")
        argv.append(request.prompt)

        started = time.monotonic()
        try:
            completed = self._run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _cleanup_temporary(temporary)
            raise ProviderTimeoutError() from None
        except FileNotFoundError:
            _cleanup_temporary(temporary)
            raise ProviderUnavailableError() from None
        except OSError:
            _cleanup_temporary(temporary)
            raise ProviderInvocationError() from None

        try:
            stdout = getattr(completed, "stdout", "")
            return_code = getattr(completed, "returncode", 1)
            if not isinstance(stdout, str):
                raise ProviderInvocationError()
            parsed = _parse_jsonl(stdout, request.model_id, _elapsed_ms(started))
            if return_code != 0:
                raise ProviderInvocationError()
            self._last_metrics = parsed.metrics
            return parsed.response
        finally:
            _cleanup_temporary(temporary)


class _ParsedOpenCode:
    def __init__(self, response: ModelResponse, metrics: OpenCodeCallMetrics):
        self.response = response
        self.metrics = metrics


def _parse_jsonl(stdout: str, model_id: str, latency_ms: int) -> _ParsedOpenCode:
    text_parts: List[str] = []
    text_part_slots: Dict[str, int] = {}
    request_id: Optional[str] = None
    finish_reason: Optional[str] = None
    input_tokens = output_tokens = reasoning_tokens = 0
    cache_read_tokens = cache_write_tokens = 0
    reported_cost: Optional[float] = None
    saw_event = False

    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            raise ProviderInvocationError() from None
        if not isinstance(event, dict):
            raise ProviderInvocationError()
        saw_event = True

        if event.get("type") in {"error", "provider_error"} or "error" in event:
            raise ProviderInvocationError()
        request_id = request_id or _first_string(
            event, "request_id", "requestId", "sessionID", "session_id", "id"
        )

        if event.get("type") == "step_finish":
            part = event.get("part") if isinstance(event.get("part"), dict) else event
            tokens = part.get("tokens", {})
            if tokens is None:
                tokens = {}
            if not isinstance(tokens, dict):
                raise ProviderInvocationError()
            input_tokens += _event_int(tokens.get("input", 0))
            output_tokens += _event_int(tokens.get("output", 0))
            reasoning_tokens += _event_int(tokens.get("reasoning", 0))
            cache = tokens.get("cache", {})
            if cache is None:
                cache = {}
            if not isinstance(cache, dict):
                raise ProviderInvocationError()
            cache_read_tokens += _event_int(cache.get("read", 0))
            cache_write_tokens += _event_int(cache.get("write", 0))
            if "cost" in part:
                reported_cost = _sum_cost(reported_cost, part.get("cost"))
            if isinstance(part.get("reason"), str):
                finish_reason = part["reason"]

        text = _text_from_event(event)
        if text:
            part = event.get("part")
            part_id = part.get("id") if isinstance(part, dict) else None
            if isinstance(part_id, str) and part_id:
                slot = text_part_slots.get(part_id)
                if slot is None:
                    text_part_slots[part_id] = len(text_parts)
                    text_parts.append(text)
                else:
                    text_parts[slot] = text
            else:
                text_parts.append(text)

    if not saw_event:
        raise ProviderInvocationError()

    response = ModelResponse(
        text="".join(text_parts),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_request_id=request_id,
        latency_ms=latency_ms,
        raw_finish_reason=finish_reason,
        provider_reported_cost=reported_cost,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        provider_pricing_is_variable=True,
    )
    metrics = OpenCodeCallMetrics(
        model_id=model_id,
        request_id=request_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        provider_reported_cost=reported_cost,
        latency_ms=latency_ms,
    )
    return _ParsedOpenCode(response, metrics)


def _text_from_event(event: Dict[str, Any]) -> str:
    if event.get("type") not in {"text", "assistant", "message"}:
        return ""
    for candidate in (
        event.get("text"),
        event.get("content"),
        event.get("part", {}).get("text")
        if isinstance(event.get("part"), dict)
        else None,
    ):
        if isinstance(candidate, str):
            return candidate
        if isinstance(candidate, list):
            chunks = [
                item.get("text", "")
                for item in candidate
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return "".join(chunks)
    return ""


def _event_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProviderInvocationError()
    return value


def _sum_cost(current: Optional[float], value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ProviderInvocationError()
    return float(value if current is None else current + value)


def _first_string(event: Dict[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = event.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _working_directory(request: ModelRequest) -> Tuple[Path, Optional[tempfile.TemporaryDirectory]]:
    requested = request.metadata.get("cwd")
    if requested is not None:
        if not isinstance(requested, (str, os.PathLike)):
            raise ProviderInvocationError()
        path = Path(requested)
        if not path.is_dir():
            raise ProviderInvocationError()
        return path, None

    temporary = tempfile.TemporaryDirectory(prefix="jev-opencode-")
    return Path(temporary.name), temporary


def _cleanup_temporary(temporary: Optional[tempfile.TemporaryDirectory]) -> None:
    if temporary is not None:
        temporary.cleanup()


def _elapsed_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))
