"""T-03 lightweight rule engine contract tests."""

import pytest

from jev_router.rules import (
    MAXIMUM_DOWNGRADE_STEPS,
    LightweightRuleEngine,
    RuleResult,
    evaluate_rule_candidate,
    is_downgrade_allowed,
)
from jev_router.schemas import CapabilityLevel, RouteRequest


PLAIN_ENGLISH_NOUNS = (
    "it",
    "this",
    "that",
    "me",
    "here",
    "more",
    "all",
    "something",
    "everything",
    "document",
    "text",
    "data",
    "code",
    "log",
    "page",
    "notes",
    "info",
    "image",
    "video",
    "json",
    "yaml",
    "csv",
    "script",
)


def make_request(prompt: str, **overrides) -> RouteRequest:
    payload = {
        "task_id": "task_rules",
        "prompt": prompt,
    }
    payload.update(overrides)
    return RouteRequest.model_validate(payload)


def test_explicit_single_file_read_hits_whitelist_with_traceable_rule_id():
    result = LightweightRuleEngine().evaluate(make_request("读取 README.md"))

    assert isinstance(result, RuleResult)
    assert result.matched is True
    assert result.rule_id == "simple_file_read"
    assert result.confidence >= 0.95
    assert result.capability is CapabilityLevel.LOW
    assert "只读" in result.reason


def test_english_single_file_read_hits_whitelist():
    result = LightweightRuleEngine().evaluate(
        make_request("Read the file src/jev_router/schemas.py")
    )

    assert result.matched is True
    assert result.rule_id == "simple_file_read"


@pytest.mark.parametrize(
    "prompt",
    ["read the file", "open file", "view file", "read the file contents"],
)
def test_ambiguous_english_read_without_context_defers_to_jev(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is False
    assert result.rule_id is None
    assert "defer to JEV" in result.reason
    assert "file" not in result.reason
    assert "contents" not in result.reason


def test_generic_english_read_uses_single_context_path():
    result = LightweightRuleEngine().evaluate(
        make_request("read the file", context=["src/a.py"])
    )

    assert result.matched is True
    assert result.rule_id == "simple_file_read"
    assert "src/a.py" in result.reason


@pytest.mark.parametrize(
    "prompt",
    [
        "display the file",
        "print file",
        "read the files",
        "read contents",
        "view the contents",
        "open the files",
    ],
)
def test_bare_english_nouns_without_file_identifier_defer_to_jev(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is False
    assert result.rule_id is None
    assert "defer to JEV" in result.reason


@pytest.mark.parametrize(
    "prompt",
    [
        "read the file",
        "view file",
        "show file",
        "open file",
        "display the file",
        "print file",
    ],
)
def test_generic_read_variants_use_one_safe_context_path(prompt):
    result = LightweightRuleEngine().evaluate(
        make_request(prompt, context=["src/a.py"])
    )

    assert result.matched is True
    assert result.rule_id == "simple_file_read"
    assert "src/a.py" in result.reason


@pytest.mark.parametrize(
    "prompt",
    ["display README.md", "print src/jev_router/schemas.py"],
)
def test_display_and_print_real_paths_hit_whitelist(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is True
    assert result.rule_id == "simple_file_read"


@pytest.mark.parametrize(
    "prompt",
    [
        f"{verb} {noun}"
        for verb in ("read", "view", "show", "open", "display", "print")
        for noun in PLAIN_ENGLISH_NOUNS
    ],
)
def test_plain_english_noun_is_not_an_explicit_file_path(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is False
    assert result.rule_id is None
    assert "defer to JEV" in result.reason


@pytest.mark.parametrize("context_path", PLAIN_ENGLISH_NOUNS)
def test_plain_english_noun_context_is_not_a_file_path(context_path):
    result = LightweightRuleEngine().evaluate(
        make_request("read the file", context=[context_path])
    )

    assert result.matched is False
    assert result.rule_id is None
    assert "defer to JEV" in result.reason


@pytest.mark.parametrize(
    "prompt",
    [
        "display README.md",
        "print src/jev_router/schemas.py",
        "read tests/test_rules.py",
        "read LICENSE",
        "read README",
        "read Makefile",
        "read Dockerfile",
    ],
)
def test_real_file_path_features_still_hit_whitelist(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is True
    assert result.rule_id == "simple_file_read"


@pytest.mark.parametrize(
    "prompt",
    [
        "read ../etc/passwd",
        "read ../../../../etc/shadow",
        "read /etc/passwd",
        "read .env",
        "read .ssh/id_rsa",
    ],
)
def test_unsafe_paths_defer_to_jev(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is False
    assert "defer to JEV" in result.reason


@pytest.mark.parametrize(
    "prompt",
    [
        "read test.py",
        "read bug_report.md",
        "read error.log",
        "read search.md",
        "read tool.md",
        "read fix.md",
    ],
)
def test_safe_filename_words_do_not_trigger_complexity_guard(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is True
    assert result.rule_id == "simple_file_read"


def test_chinese_read_content_phrase_allows_normal_spacing():
    result = LightweightRuleEngine().evaluate(make_request("读取 README.md 的内容"))

    assert result.matched is True
    assert result.rule_id == "simple_file_read"


def test_explicit_file_from_single_context_item_can_hit_read_rule():
    result = LightweightRuleEngine().evaluate(
        make_request("读取文件内容", context=["README.md"])
    )

    assert result.matched is True
    assert result.rule_id == "simple_file_read"


def test_explicit_file_list_hits_low_risk_whitelist():
    result = LightweightRuleEngine().evaluate(make_request("列出当前目录文件"))

    assert result.matched is True
    assert result.rule_id == "simple_file_list"
    assert result.capability is CapabilityLevel.LOW


@pytest.mark.parametrize(
    "prompt",
    [
        "debug 登录失败并修复错误",
        "修改 README.md 并运行测试",
        "更新 src/a.py 和 src/b.py",
        "设计认证架构",
        "做安全审查",
        "执行数据迁移",
        "搜索外部资料后比较方案",
        "分析这个未知错误",
        "调用多个工具完成任务",
        "完成目标",
    ],
)
def test_complex_or_ambiguous_tasks_do_not_hit_lightweight_rules(prompt):
    result = LightweightRuleEngine().evaluate(make_request(prompt))

    assert result.matched is False
    assert "defer to JEV" in result.reason


def test_multiple_context_items_are_not_lightweight():
    result = LightweightRuleEngine().evaluate(
        make_request("读取文件内容", context=["README.md", "pyproject.toml"])
    )

    assert result.matched is False
    assert "context" in result.reason


def test_long_context_is_not_lightweight():
    result = LightweightRuleEngine().evaluate(
        make_request("读取 README.md", context=["a" * 241])
    )

    assert result.matched is False
    assert "context" in result.reason


def test_maximum_downgrade_steps_is_one():
    assert MAXIMUM_DOWNGRADE_STEPS == 1
    assert is_downgrade_allowed(CapabilityLevel.MEDIUM, CapabilityLevel.LOW)
    assert is_downgrade_allowed(CapabilityLevel.HIGH, CapabilityLevel.MEDIUM)
    assert not is_downgrade_allowed(CapabilityLevel.HIGH, CapabilityLevel.LOW)


def test_low_confidence_candidate_is_rejected_with_rule_id_trace():
    result = evaluate_rule_candidate(
        rule_id="simple_file_read",
        confidence=0.94,
        capability=CapabilityLevel.LOW,
        reason="candidate read",
    )

    assert result.matched is False
    assert result.rule_id == "simple_file_read"
    assert result.confidence == 0.0
    assert "confidence=0.94" in result.reason
    assert "defer to JEV" in result.reason


def test_high_to_low_rule_downgrade_is_rejected_and_traceable():
    result = LightweightRuleEngine().evaluate(
        make_request("读取 README.md"),
        current_capability=CapabilityLevel.HIGH,
    )

    assert result.matched is False
    assert result.rule_id == "simple_file_read"
    assert "maximum_downgrade_steps=1" in result.reason
    assert "high->low" in result.reason


def test_medium_to_low_rule_downgrade_is_allowed():
    result = LightweightRuleEngine().evaluate(
        make_request("读取 README.md"),
        current_capability=CapabilityLevel.MEDIUM,
    )

    assert result.matched is True
    assert result.capability is CapabilityLevel.LOW


def test_unmatched_result_is_explicit():
    result = LightweightRuleEngine().evaluate(make_request("请帮我处理一下"))

    assert result == RuleResult(
        matched=False,
        rule_id=None,
        confidence=0.0,
        capability=None,
        reason="No whitelisted lightweight rule matched; defer to JEV.",
    )
