import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_router.opencode_go import OpenCodeGoProvider
from jev_router.providers import (
    ModelRequest,
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


def request(**overrides):
    payload = {
        "model_id": "opencode-go/qwen3.8-flash",
        "prompt": "answer without echoing this secret-like prompt",
    }
    payload.update(overrides)
    return ModelRequest.model_validate(payload)


def jsonl_events():
    return "\n".join(
        [
            json.dumps({"type": "text", "part": {"text": "first "}}),
            json.dumps(
                {
                    "type": "step_finish",
                    "id": "step-1",
                    "part": {
                        "reason": "tool",
                        "cost": 0.12,
                        "tokens": {
                            "input": 10,
                            "output": 4,
                            "reasoning": 2,
                            "cache": {"read": 3, "write": 1},
                        },
                    },
                }
            ),
            json.dumps({"type": "text", "part": {"text": "answer"}}),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "reason": "stop",
                        "cost": 0.03,
                        "tokens": {
                            "input": 5,
                            "output": 6,
                            "reasoning": 1,
                            "cache": {"read": 7, "write": 2},
                        },
                    },
                }
            ),
        ]
    )


def test_opencode_go_uses_argv_and_parses_usage_cache_and_reported_cost(tmp_path):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=jsonl_events(), stderr="secret")

    provider = OpenCodeGoProvider(run=run, timeout=9)
    response = provider.invoke(
        request(metadata={"cwd": str(tmp_path), "auto": True})
    )

    assert response.text == "first answer"
    assert response.input_tokens == 15
    assert response.output_tokens == 10
    assert response.reasoning_tokens == 3
    assert response.cache_read_tokens == 10
    assert response.cache_write_tokens == 3
    assert response.provider_reported_cost == 0.15
    assert response.provider_pricing_is_variable is True
    assert response.raw_finish_reason == "stop"
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == [
        "opencode",
        "run",
        "--dir",
        str(tmp_path),
        "-m",
        "opencode-go/qwen3.8-flash",
        "--format",
        "json",
        "--pure",
        "--auto",
        "answer without echoing this secret-like prompt",
    ]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["timeout"] == 9
    assert kwargs["capture_output"] is True
    assert provider.last_call_metrics["provider_reported_cost"] == 0.15


def test_opencode_go_default_cwd_is_fresh_and_prompt_is_one_argv_item():
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["cwd"]))
        assert Path(kwargs["cwd"]).is_dir()
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "text", "text": "ok"}),
            stderr="",
        )

    provider = OpenCodeGoProvider(run=run)
    provider.invoke(request(metadata={"auto": False}))

    assert seen[0][0][-1] == "answer without echoing this secret-like prompt"
    assert "--auto" not in seen[0][0]
    assert not Path(seen[0][1]).exists()


def test_opencode_go_rejects_invalid_cwd_without_running_cli(tmp_path):
    calls = []
    provider = OpenCodeGoProvider(run=lambda *args, **kwargs: calls.append(args))

    with pytest.raises(ProviderInvocationError):
        provider.invoke(request(metadata={"cwd": str(tmp_path / "missing")}))

    assert calls == []


def test_opencode_go_timeout_does_not_echo_prompt():
    secret_prompt = "prompt-secret-never-in-error"

    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    provider = OpenCodeGoProvider(run=run)
    with pytest.raises(ProviderTimeoutError) as exc_info:
        provider.invoke(request(prompt=secret_prompt))

    assert str(exc_info.value) == "Provider request timed out"
    assert secret_prompt not in str(exc_info.value)


def test_opencode_go_provider_error_event_and_missing_cli_are_stable():
    provider = OpenCodeGoProvider(
        run=lambda argv, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"type": "error", "message": "secret"}),
            stderr="secret",
        )
    )
    with pytest.raises(ProviderInvocationError) as exc_info:
        provider.invoke(request())
    assert str(exc_info.value) == "Provider invocation failed"

    missing = OpenCodeGoProvider(
        run=lambda argv, **kwargs: (_ for _ in ()).throw(
            FileNotFoundError("/secret/path")
        )
    )
    with pytest.raises(ProviderUnavailableError) as missing_info:
        missing.invoke(request())
    assert str(missing_info.value) == "Provider model unavailable"
