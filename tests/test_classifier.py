"""T-04 JEV classifier boundary tests."""

import json
from pathlib import Path

import pytest

from jev_router.classifier import CLASSIFIER_PROMPT_VERSION, JEVClassifier
from jev_router.errors import SchemaValidationError
from jev_router.schemas import ClassifierResult, RouteRequest


PROJECT_ROOT = Path(__file__).parents[1]
PROMPT_PATH = PROJECT_ROOT / "config" / "prompts" / "jev_classifier_v0.1.txt"


def make_request(**overrides):
    payload = {
        "task_id": "task_004",
        "prompt": "修复登录失败问题并运行测试",
        "context": ["src/auth.py", "tests/test_auth.py"],
        "metadata": {"source": "test", "secret": "test-only-secret"},
    }
    payload.update(overrides)
    return RouteRequest.model_validate(payload)


def classifier_payload(**overrides):
    payload = {
        "schema_version": "0.1",
        "task_type": "debugging",
        "difficulty_score": 6,
        "difficulty_bucket": "medium",
        "required_capability": "medium",
        "confidence": 0.86,
        "risk_level": "medium",
        "notes": "涉及多文件定位和回归测试",
    }
    payload.update(overrides)
    return payload


def test_valid_json_is_parsed_as_classifier_result_and_prompt_is_assembled():
    prompts = []

    def call_model(prompt):
        prompts.append(prompt)
        return json.dumps(classifier_payload(), ensure_ascii=False)

    classifier = JEVClassifier(call_model)
    result = classifier.classify(make_request())

    assert isinstance(result, ClassifierResult)
    assert result.task_type.value == "debugging"
    assert len(prompts) == 1
    assert "修复登录失败问题并运行测试" in prompts[0]
    assert "src/auth.py" in prompts[0]
    assert "test-only-secret" not in prompts[0]


def test_classifier_prompt_version_is_exposed_and_matches_prompt_file():
    classifier = JEVClassifier(lambda prompt: json.dumps(classifier_payload()))

    assert CLASSIFIER_PROMPT_VERSION == "v0.1"
    assert classifier.classifier_prompt_version == "v0.1"
    assert PROMPT_PATH.name == "jev_classifier_v0.1.txt"
    prompt_text = PROMPT_PATH.read_text(encoding="utf-8")
    assert "v0.1" in prompt_text


def test_non_json_response_raises_stable_schema_error_without_echoing_response():
    secret = "test-response-secret"
    classifier = JEVClassifier(lambda prompt: f"not json {secret}")

    with pytest.raises(SchemaValidationError) as exc_info:
        classifier.classify(make_request())

    assert "not valid JSON" in str(exc_info.value)
    assert secret not in str(exc_info.value)


def test_missing_required_field_raises_stable_schema_error():
    payload = classifier_payload()
    del payload["difficulty_bucket"]
    classifier = JEVClassifier(lambda prompt: json.dumps(payload))

    with pytest.raises(SchemaValidationError, match="schema validation"):
        classifier.classify(make_request())


@pytest.mark.parametrize(
    "override",
    [
        {"task_type": "not-a-task-type"},
        {"difficulty_score": 0},
        {"difficulty_score": 11},
        {"difficulty_bucket": "extreme"},
        {"required_capability": "extreme"},
        {"confidence": -0.01},
        {"confidence": 1.01},
        {"risk_level": "extreme"},
    ],
)
def test_invalid_enum_or_range_raises_stable_schema_error(override):
    classifier = JEVClassifier(
        lambda prompt: json.dumps(classifier_payload(**override))
    )

    with pytest.raises(SchemaValidationError, match="schema validation"):
        classifier.classify(make_request())


def test_schema_extra_field_is_rejected():
    classifier = JEVClassifier(
        lambda prompt: json.dumps(classifier_payload(unexpected="rejected"))
    )

    with pytest.raises(SchemaValidationError, match="schema validation"):
        classifier.classify(make_request())


def test_prompt_contains_classifier_boundary_requirements():
    prompts = []
    classifier = JEVClassifier(
        lambda prompt: prompts.append(prompt) or json.dumps(classifier_payload())
    )

    classifier.classify(make_request())
    prompt = prompts[0]

    required_phrases = (
        "Do not select a model",
        "Return exactly one JSON object",
        "Use only the current input",
        "Do not use model price",
        "Do not add fields outside this schema",
        "task_type: one of",
        "difficulty_score: integer from 1 to 10",
        "confidence: number from 0.0 to 1.0",
    )
    for phrase in required_phrases:
        assert phrase in prompt
    for task_type in (
        "file_operation",
        "coding",
        "debugging",
        "testing",
        "review",
        "architecture",
        "research",
        "reasoning",
        "other",
    ):
        assert f'"{task_type}"' in prompt
