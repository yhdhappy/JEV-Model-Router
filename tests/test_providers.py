"""T-05 provider and deterministic mock-provider contract tests."""

import pytest
from pydantic import ValidationError

from jev_router.providers import (
    MockProvider,
    MockScenario,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderInvocationError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)


def make_request(**overrides):
    payload = {
        "model_id": "medium-model",
        "prompt": "return a short answer",
    }
    payload.update(overrides)
    return ModelRequest.model_validate(payload)


def test_model_request_rejects_unknown_fields_and_requires_model_and_prompt():
    with pytest.raises(ValidationError):
        ModelRequest.model_validate(
            {"model_id": "medium-model", "prompt": "hello", "unexpected": True}
        )

    with pytest.raises(ValidationError):
        ModelRequest.model_validate({"model_id": "medium-model", "prompt": ""})


def test_model_provider_protocol_and_success_response_contract_are_stable():
    request = make_request()
    provider = MockProvider()

    assert isinstance(provider, ModelProvider)

    first = provider.invoke(request)
    second = provider.invoke(request)

    assert isinstance(first, ModelResponse)
    assert first == second
    assert first.text == "mock response"
    assert first.input_tokens == 1
    assert first.output_tokens == 2
    assert first.provider_request_id == "mock-provider-request"
    assert first.latency_ms == 0
    assert first.raw_finish_reason == "stop"


@pytest.mark.parametrize(
    "scenario,error_type,error_code,message",
    [
        (
            MockScenario.TIMEOUT,
            ProviderTimeoutError,
            "provider_timeout",
            "Provider request timed out",
        ),
        (
            MockScenario.UNAVAILABLE,
            ProviderUnavailableError,
            "model_unavailable",
            "Provider model unavailable",
        ),
        (
            MockScenario.ERROR,
            ProviderInvocationError,
            "provider_error",
            "Provider invocation failed",
        ),
    ],
)
def test_mock_failure_scenarios_have_distinct_stable_errors(
    scenario, error_type, error_code, message
):
    secret_like_prompt = "do not echo this secret-like text"
    provider = MockProvider([scenario])

    with pytest.raises(error_type) as exc_info:
        provider.invoke(make_request(prompt=secret_like_prompt))

    assert exc_info.value.code == error_code
    assert str(exc_info.value) == message
    assert secret_like_prompt not in str(exc_info.value)


def test_mock_outcomes_are_consumed_in_order_and_last_outcome_is_repeated():
    response = ModelResponse(
        text="fallback answer",
        input_tokens=3,
        output_tokens=4,
        provider_request_id="mock-fallback-request",
        latency_ms=7,
        raw_finish_reason="stop",
    )
    provider = MockProvider(
        [MockScenario.UNAVAILABLE, response, MockScenario.TIMEOUT]
    )
    request = make_request()

    with pytest.raises(ProviderUnavailableError):
        provider.invoke(request)
    assert provider.invoke(request) == response
    with pytest.raises(ProviderTimeoutError):
        provider.invoke(request)
    with pytest.raises(ProviderTimeoutError):
        provider.invoke(request)

    assert provider.call_count == 4
    assert provider.requests == (request, request, request, request)


def test_model_response_accepts_optional_metadata_and_rejects_negative_usage():
    response = ModelResponse(
        text="answer",
        input_tokens=0,
        output_tokens=0,
        provider_request_id=None,
        latency_ms=None,
        raw_finish_reason=None,
    )

    assert response.model_dump() == {
        "text": "answer",
        "input_tokens": 0,
        "output_tokens": 0,
        "provider_request_id": None,
        "latency_ms": None,
        "raw_finish_reason": None,
    }

    with pytest.raises(ValidationError):
        ModelResponse(text="answer", input_tokens=-1, output_tokens=0)
