import json
import socket
from pathlib import Path

import pytest

from jev_router.errors import ConfigurationError, SchemaValidationError
from jev_router.providers import ProviderInvocationError, ProviderTimeoutError
from jev_router.real_jev import (
    RealJEVClassifier,
    _question_schema,
)
from jev_router.schemas import RouteRequest


class FakeHTTPResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def close(self):
        pass


def make_request(**overrides):
    payload = {
        "task_id": "real-jev-test",
        "prompt": "debug the failing login flow",
        "context": ["src/auth.py"],
        "metadata": {"secret": "must-not-be-sent"},
    }
    payload.update(overrides)
    return RouteRequest.model_validate(payload)


def answer_payload(score=4.5):
    return {
        "model": "jev-1.13.0",
        "request_id": "jev-request-1",
        "answers": {
            "task_type": {"choice": "debugging", "confidence": 0.91},
            "difficulty_score": {"score": score, "confidence": 0.61},
            "difficulty_bucket": {"choice": "medium", "confidence": 0.77},
            "required_capability": {"choice": "medium", "confidence": 0.88},
            "risk_level": {"choice": "high", "confidence": 0.72},
        },
        "usage": {"input_tokens": 1237, "output_tokens": 88},
    }


def write_key(tmp_path: Path, key="jev-test-secret"):
    path = tmp_path / "jev-key.rtf"
    path.write_text(r"{\rtf1\ansi JEV_API_KEY=" + key + r"}", encoding="utf-8")
    return path


def test_real_jev_posts_one_typed_request_and_maps_classifier_result(tmp_path):
    calls = []

    def urlopen(request, timeout):
        calls.append((request, timeout))
        return FakeHTTPResponse(answer_payload())

    classifier = RealJEVClassifier(
        str(write_key(tmp_path)), urlopen=urlopen, timeout=7
    )
    result = classifier.classify(make_request())

    assert result.task_type.value == "debugging"
    assert result.difficulty_score == 6
    assert result.difficulty_bucket.value == "medium"
    assert result.required_capability.value == "medium"
    assert result.risk_level.value == "high"
    assert result.confidence == 0.61
    assert len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://api.typesafe.ai/v1/systemone"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("Authorization") == "Bearer jev-test-secret"
    body = json.loads(request.data.decode("utf-8"))
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == "jev-latest"
    assert body["state"] == {
        "prompt": "debug the failing login flow",
        "context": ["src/auth.py"],
    }
    assert "must-not-be-sent" not in request.data.decode("utf-8")
    assert set(body["questions"]) == {
        "task_type",
        "difficulty_score",
        "difficulty_bucket",
        "required_capability",
        "risk_level",
    }
    assert list(body["questions"]["task_type"]["criteria"]) == [
        "file_operation",
        "coding",
        "debugging",
        "testing",
        "review",
        "architecture",
        "research",
        "reasoning",
        "other",
    ]
    assert len(body["questions"]["difficulty_score"]["criteria"]) == 10
    assert timeout == 7


def test_real_jev_cost_resolver_uses_exact_input_usage(tmp_path):
    classifier = RealJEVClassifier(
        str(write_key(tmp_path)),
        urlopen=lambda request, timeout: FakeHTTPResponse(answer_payload()),
    )
    request = make_request()
    result = classifier.classify(request)
    component = classifier.resolve_cost(request, result)

    assert component.cost == 1237 * 0.042 / 1_000_000
    assert component.cost_estimated is False
    assert component.cost_estimation_source == "provider_usage"
    assert classifier.last_call_metrics["returned_model"] == "jev-1.13.0"
    assert classifier.last_call_metrics["request_id"] == "jev-request-1"
    assert classifier.last_call_metrics["input_tokens"] == 1237
    assert classifier.last_call_metrics["output_tokens"] == 88
    assert classifier.last_call_metrics["exact_cost"] == component.cost


@pytest.mark.parametrize("failure_kind", ["timeout", "network", "schema"])
def test_real_jev_failed_attempt_clears_previous_metrics_and_cost(
    tmp_path, failure_kind
):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            return FakeHTTPResponse(answer_payload())
        if failure_kind == "timeout":
            raise socket.timeout()
        if failure_kind == "network":
            raise OSError("network detail must stay hidden")
        return FakeHTTPResponse(
            {"answers": {}, "usage": {"input_tokens": 1, "output_tokens": 0}}
        )

    classifier = RealJEVClassifier(str(write_key(tmp_path)), urlopen=urlopen)
    request = make_request()
    first_result = classifier.classify(request)
    assert classifier.last_call_metrics is not None

    expected_error = {
        "timeout": ProviderTimeoutError,
        "network": ConnectionError,
        "schema": SchemaValidationError,
    }[failure_kind]
    with pytest.raises(expected_error):
        classifier.classify(request)

    assert classifier.last_call_metrics is None
    with pytest.raises(ValueError, match="JEV usage is unavailable"):
        classifier.resolve_cost(request, first_result)


def test_real_jev_rejects_malformed_payload_with_stable_error(tmp_path):
    classifier = RealJEVClassifier(
        str(write_key(tmp_path)),
        urlopen=lambda request, timeout: FakeHTTPResponse(
            {"answers": {}, "usage": {"input_tokens": 1, "output_tokens": 0}}
        ),
    )

    with pytest.raises(SchemaValidationError) as exc_info:
        classifier.classify(make_request())

    assert str(exc_info.value) == "JEV classifier response failed schema validation"


@pytest.mark.parametrize("failure", [socket.timeout(), TimeoutError()])
def test_real_jev_timeout_is_stable(tmp_path, failure):
    classifier = RealJEVClassifier(
        str(write_key(tmp_path)),
        urlopen=lambda request, timeout: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(ProviderTimeoutError) as exc_info:
        classifier.classify(make_request())

    assert str(exc_info.value) == "Provider request timed out"


def test_real_jev_network_error_and_secret_redaction(tmp_path):
    secret = "jev-secret-never-logged"
    classifier = RealJEVClassifier(
        str(write_key(tmp_path, secret)),
        urlopen=lambda request, timeout: (_ for _ in ()).throw(
            OSError(secret)
        ),
    )

    with pytest.raises(ConnectionError) as exc_info:
        classifier.classify(make_request())

    assert secret not in str(exc_info.value)
    assert secret not in repr(classifier)
    assert secret not in str(classifier.last_call_metrics)


def test_real_jev_question_schema_has_exact_level_choices():
    questions = _question_schema()
    assert set(questions["difficulty_bucket"]["criteria"]) == {
        "low",
        "medium",
        "high",
    }
    assert set(questions["required_capability"]["criteria"]) == {
        "low",
        "medium",
        "high",
    }
    assert set(questions["risk_level"]["criteria"]) == {
        "low",
        "medium",
        "high",
    }


def test_real_jev_missing_key_path_fails_without_reading_a_default():
    with pytest.raises(ConfigurationError):
        RealJEVClassifier(api_key_file="/path/that/does/not/exist")


def test_real_jev_rtf_parser_ignores_font_table_metadata(tmp_path):
    path = tmp_path / "jev-key-with-fonts.rtf"
    path.write_text(
        r"{\rtf1\ansi{\fonttbl{\f0 Calibri;}}\pard\f0 "
        r"JEV_API_KEY=jev-font-table-secret}",
        encoding="utf-8",
    )
    classifier = RealJEVClassifier(
        str(path),
        urlopen=lambda request, timeout: FakeHTTPResponse(answer_payload()),
    )

    assert "api_key=<redacted>" in repr(classifier)
    assert classifier.classify(make_request()).task_type.value == "debugging"
