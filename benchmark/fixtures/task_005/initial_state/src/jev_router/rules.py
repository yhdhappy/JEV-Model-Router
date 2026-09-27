"""Conservative, whitelist-only lightweight routing rules.

The rule engine is intentionally narrower than a classifier.  It recognizes
only explicit, low-risk read/list requests and otherwise defers to JEV.
"""

import re
from typing import Optional, Sequence, Tuple, Union

from pydantic import Field

from .schemas import CapabilityLevel, RouteRequest, StrictModel


MAXIMUM_DOWNGRADE_STEPS = 1
HIGH_CONFIDENCE_THRESHOLD = 0.95
_LIGHTWEIGHT_CAPABILITY = CapabilityLevel.LOW

_CAPABILITY_RANK = {
    CapabilityLevel.LOW: 0,
    CapabilityLevel.MEDIUM: 1,
    CapabilityLevel.HIGH: 2,
}

_SIMPLE_PATH = r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+"
_EXTENSIONLESS_FILE_NAMES = frozenset(
    {"README", "LICENSE", "Makefile", "Dockerfile"}
)
_READ_PATTERNS = (
    re.compile(
        rf"^(?:please\s+)?(?:read|view|show|open|display|print)\s+"
        rf"(?:the\s+)?(?:file\s+)?(?P<path>{_SIMPLE_PATH})"
        rf"(?:\s+contents?)?\.?$",
        re.IGNORECASE,
    ),
    re.compile(
        rf"^(?:请)?(?:读取|查看|打开|显示|打印)(?:一下)?"
        rf"(?:文件)?\s*(?P<path>{_SIMPLE_PATH})\s*(?:的内容)?[。.]?$"
    ),
)
_GENERIC_READ_PATTERNS = (
    re.compile(
        r"^(?:please\s+)?(?:read|view|show|open|display|print)\s+"
        r"(?:(?:the|a|an)\s+)?file(?:\s+contents?)?\.?$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:请)?(?:读取|查看|打开)(?:一下)?(?:文件)?(?:内容)?[。.]?$"),
)
_LIST_PATTERNS = (
    re.compile(r"^(?:please\s+)?list\s+(?:the\s+)?files?\.?$", re.IGNORECASE),
    re.compile(r"^(?:请)?列出(?:当前)?目录文件[。.]?$"),
)

_COMPLEX_MARKERS = (
    "debug",
    "debugging",
    "fix",
    "error",
    "failure",
    "bug",
    "test",
    "modify",
    "change",
    "edit",
    "write",
    "create",
    "delete",
    "remove",
    "rename",
    "migrat",
    "architect",
    "security",
    "research",
    "search",
    "external",
    "tool",
    "multiple",
    "multi-file",
    "long context",
    "调试",
    "修复",
    "错误",
    "失败",
    "故障",
    "测试",
    "修改",
    "更改",
    "编辑",
    "写入",
    "创建",
    "删除",
    "移除",
    "重命名",
    "迁移",
    "架构",
    "安全",
    "研究",
    "搜索",
    "外部",
    "工具",
    "多个",
    "多文件",
    "长上下文",
    "分析",
    "比较",
    "设计",
)


class RuleResult(StrictModel):
    """Auditable result returned by the lightweight rule engine."""

    matched: bool
    rule_id: Optional[str] = Field(default=None, min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    capability: Optional[CapabilityLevel] = None
    reason: str = Field(min_length=1)


# A descriptive alias keeps the result easy to discover for callers that use
# "decision" terminology while the public schema follows the existing
# ClassifierResult/RouteResult naming convention.
RuleDecision = RuleResult


def downgrade_steps(
    from_capability: Union[CapabilityLevel, str],
    to_capability: Union[CapabilityLevel, str],
) -> int:
    """Return the number of capability levels removed by a route."""

    source = _coerce_capability(from_capability)
    target = _coerce_capability(to_capability)
    return max(_CAPABILITY_RANK[source] - _CAPABILITY_RANK[target], 0)


def is_downgrade_allowed(
    from_capability: Union[CapabilityLevel, str],
    to_capability: Union[CapabilityLevel, str],
) -> bool:
    """Apply the fixed one-level downgrade guard.

    Upgrades and same-level routes are allowed.  A downgrade is allowed only
    when it removes at most ``MAXIMUM_DOWNGRADE_STEPS`` capability level.
    """

    return downgrade_steps(from_capability, to_capability) <= MAXIMUM_DOWNGRADE_STEPS


def evaluate_rule_candidate(
    *,
    rule_id: str,
    confidence: float,
    capability: Union[CapabilityLevel, str],
    reason: str,
    current_capability: Optional[Union[CapabilityLevel, str]] = None,
) -> RuleResult:
    """Apply confidence and downgrade guards to one rule candidate."""

    if confidence < HIGH_CONFIDENCE_THRESHOLD:
        return _unmatched(
            f"{rule_id} confidence={confidence:.2f} is below the "
            "high-confidence guard; defer to JEV.",
            rule_id=rule_id,
        )

    target = _coerce_capability(capability)
    if current_capability is not None and not is_downgrade_allowed(
        current_capability, target
    ):
        source = _coerce_capability(current_capability).value
        return _unmatched(
            f"{rule_id} rejected: maximum_downgrade_steps=1 forbids "
            f"{source}->{target.value}; defer to JEV.",
            rule_id=rule_id,
        )

    return RuleResult(
        matched=True,
        rule_id=rule_id,
        confidence=confidence,
        capability=target,
        reason=reason,
    )


class LightweightRuleEngine:
    """Evaluate only explicitly allowlisted low-risk task shapes."""

    maximum_downgrade_steps = MAXIMUM_DOWNGRADE_STEPS

    def evaluate(
        self,
        request: Union[RouteRequest, str],
        *,
        current_capability: Optional[Union[CapabilityLevel, str]] = None,
    ) -> RuleResult:
        """Return a rule result; unmatched tasks are explicitly deferred.

        ``current_capability`` is optional because the first route has no
        existing capability.  When supplied, it protects against a rule
        silently jumping from high to low capability.
        """

        normalized_request = _coerce_request(request)
        prompt = _normalize_text(normalized_request.prompt)

        complexity_reason = _complexity_guard(
            normalized_request, _semantic_prompt(prompt)
        )
        if complexity_reason is not None:
            return _unmatched(complexity_reason)

        candidate = _match_read_rule(normalized_request, prompt)
        if candidate is None:
            candidate = _match_list_rule(normalized_request, prompt)
        if candidate is None:
            return _unmatched("No whitelisted lightweight rule matched; defer to JEV.")

        rule_id, confidence, reason = candidate
        return evaluate_rule_candidate(
            rule_id=rule_id,
            confidence=confidence,
            capability=_LIGHTWEIGHT_CAPABILITY,
            reason=reason,
            current_capability=current_capability,
        )

    # ``match`` is a readable synonym for callers that treat the engine as a
    # predicate while keeping one implementation path.
    match = evaluate


def evaluate_lightweight_rules(
    request: Union[RouteRequest, str],
    *,
    current_capability: Optional[Union[CapabilityLevel, str]] = None,
) -> RuleResult:
    """Convenience function for one-off rule evaluation."""

    return LightweightRuleEngine().evaluate(
        request, current_capability=current_capability
    )


def _coerce_request(request: Union[RouteRequest, str]) -> RouteRequest:
    if isinstance(request, RouteRequest):
        return request
    if isinstance(request, str):
        return RouteRequest(task_id="rule-evaluation", prompt=request)
    raise TypeError("request must be a RouteRequest or prompt string")


def _coerce_capability(value: Union[CapabilityLevel, str]) -> CapabilityLevel:
    return value if isinstance(value, CapabilityLevel) else CapabilityLevel(value)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def _semantic_prompt(prompt: str) -> str:
    """Remove an identified path before checking semantic complexity markers."""

    for pattern in _READ_PATTERNS:
        match = pattern.fullmatch(prompt)
        if match is not None:
            start, end = match.span("path")
            return f"{prompt[:start]} {prompt[end:]}"
    return prompt


def _complexity_guard(request: RouteRequest, prompt: str) -> Optional[str]:
    if len(prompt) > 240:
        return "Task prompt is too long for a lightweight rule; defer to JEV."
    if len(request.context) > 1:
        return "Task context has multiple items; defer to JEV."
    if request.context and len(request.context[0]) > 240:
        return "Task context is too long for a lightweight rule; defer to JEV."
    lowered = prompt.casefold()
    marker = next((item for item in _COMPLEX_MARKERS if item in lowered), None)
    if marker is not None:
        return f"Complexity marker '{marker}' detected; defer to JEV."
    return None


def _match_read_rule(
    request: RouteRequest, prompt: str
) -> Optional[Tuple[str, float, str]]:
    if any(pattern.fullmatch(prompt) for pattern in _GENERIC_READ_PATTERNS):
        context_path = _context_path(request.context)
        if context_path is None:
            return None
        return (
            "simple_file_read",
            0.99,
            f"明确读取单个文件 {context_path}，只读、无修改",
        )

    path = _extract_path(_READ_PATTERNS, prompt)
    if path is None:
        return None
    if not is_explicit_file_path(path):
        return None
    context_path = _context_path(request.context)
    if request.context and context_path != path:
        return None
    return (
        "simple_file_read",
        0.99,
        f"明确读取单个文件 {path}，只读、无修改",
    )


def _match_list_rule(
    request: RouteRequest, prompt: str
) -> Optional[Tuple[str, float, str]]:
    if request.context:
        return None
    if any(pattern.fullmatch(prompt) for pattern in _LIST_PATTERNS):
        return (
            "simple_file_list",
            0.99,
            "明确列出当前目录文件，只读、无修改",
        )
    return None


def _extract_path(patterns: Sequence[re.Pattern], prompt: str) -> Optional[str]:
    for pattern in patterns:
        match = pattern.fullmatch(prompt)
        if match is not None:
            return match.group("path")
    return None


def _context_path(context: Sequence[str]) -> Optional[str]:
    if len(context) != 1:
        return None
    candidate = context[0].strip()
    if not is_explicit_file_path(candidate):
        return None
    return candidate


def is_explicit_file_path(path: str) -> bool:
    """Return whether ``path`` is both safe and recognizably a file path."""

    if not isinstance(path, str) or re.fullmatch(_SIMPLE_PATH, path) is None:
        return False
    segments = path.split("/")
    if any(segment in {".", ".."} for segment in segments):
        return False
    if any(segment.startswith(".") for segment in segments):
        return False

    basename = segments[-1]
    if "/" in path:
        return True
    if basename in _EXTENSIONLESS_FILE_NAMES:
        return True
    if "." in basename and not basename.endswith("."):
        return True

    return False


# Compatibility alias for callers that used the earlier internal helper.
_is_safe_path = is_explicit_file_path


def _unmatched(reason: str, *, rule_id: Optional[str] = None) -> RuleResult:
    return RuleResult(
        matched=False,
        rule_id=rule_id,
        confidence=0.0,
        capability=None,
        reason=reason,
    )
