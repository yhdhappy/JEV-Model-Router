"""Offline, fail-closed aggregation for the frozen Pilot Summary gate.

This module only reads the frozen Pilot config and official JSONL.  It never
constructs a provider, reads credentials, calls JEV, changes workflow state,
or chooses a Go/Adjust/Stop decision.

The two summary files are staged as same-directory temporary files, fsynced,
and replaced sequentially.  If the second replacement or directory fsync
fails, changed outputs are restored from their original bytes when possible.
This is rollback-safe for ordinary replacement failures, but cannot provide
cross-file atomicity across a process or machine crash between replacements.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from benchmark.pilot_config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_MODEL_REGISTRY_PATH,
    PilotConfig,
    PilotConfigError,
    load_pilot_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUMMARY_DECISIONS = ("go", "adjust", "stop")
DECISION_EFFECTIVENESS = ("supports", "mixed", "insufficient", "contradicts")
REAL_PASS_STATUSES = frozenset(("passed", "manual_passed"))
REAL_ACCEPTANCE_STATUSES = REAL_PASS_STATUSES | frozenset(("failed", "manual_failed"))
CONTROLLED_ACCEPTANCE_STATUS = "controlled_mock_passed"
AUDIT_ASSESSMENTS = frozenset(("reasonable", "questionable", "clearly_unreasonable", "not_applicable"))
PENDING_ACCEPTANCE = "pending_manual"
PENDING_AUDIT = "pending_human_review"
_UNSAFE_TEXT = re.compile(
    r"(?i)(?:prompt|raw|stdout|stderr|authorization|bearer|credential|api[_-]?key|"
    r"secret|/users/|/home/|~/|\bsk-[a-z0-9])"
)


class PilotSummaryError(ValueError):
    """Raised when the frozen summary input or finalization contract is invalid."""

    code = "pilot_summary_error"

    def __init__(self, detail: str):
        super().__init__(f"{self.code}: {detail}")


def build_pilot_summary(
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    results_path: Optional[Path] = None,
    model_registry_path: Path = DEFAULT_MODEL_REGISTRY_PATH,
) -> Dict[str, Any]:
    """Validate official records and return a decision-free objective summary."""

    config, root, official_path, _json_path, _markdown_path = _load_inputs(
        config_path, results_path, model_registry_path
    )
    records = _read_jsonl(official_path)
    grouped = _validate_records(records, config)
    summary = _aggregate(config, grouped, records)
    _assert_safe_projection(summary)
    return summary


def finalize_pilot_summary(
    *,
    decision: str,
    decision_effectiveness: str,
    decision_notes: str,
    config_path: Path = DEFAULT_CONFIG_PATH,
    results_path: Optional[Path] = None,
    model_registry_path: Path = DEFAULT_MODEL_REGISTRY_PATH,
) -> Dict[str, Any]:
    """Build and safely write both frozen summary outputs.

    ``decision`` and ``decision_effectiveness`` are mandatory advisor inputs;
    the builder never derives either one from metrics.
    """

    _validate_choice(decision, SUMMARY_DECISIONS, "decision")
    _validate_choice(decision_effectiveness, DECISION_EFFECTIVENESS, "decision_effectiveness")
    notes = _validate_notes(decision_notes)
    config, root, official_path, json_path, markdown_path = _load_inputs(
        config_path, results_path, model_registry_path
    )
    records = _read_jsonl(official_path)
    grouped = _validate_records(records, config)
    summary = _aggregate(config, grouped, records)
    summary.update(
        {
            "decision": decision,
            "decision_effectiveness": decision_effectiveness,
            "decision_effectiveness_notes": notes,
        }
    )
    _assert_safe_projection(summary)
    markdown = render_pilot_summary_markdown(summary)
    _atomic_write_pair(json_path, markdown_path, _json_bytes(summary), markdown.encode("utf-8"))
    return summary


def render_pilot_summary_markdown(summary: Mapping[str, Any]) -> str:
    """Render only projected summary fields using the frozen section order."""

    decision_value = summary.get("decision")
    decision = decision_value or "未设置（等待 advisor）"
    integration_text = {
        "go": "是：进入真实 Agent 集成验证。",
        "adjust": "否：先调整受影响模块并重跑受影响任务。",
        "stop": "否：暂停扩展，不进入 Adapter/UI。",
    }.get(decision_value, "未设置（等待 advisor）。")
    effectiveness = summary.get("decision_effectiveness") or "未设置（等待 advisor）"
    notes = summary.get("decision_effectiveness_notes") or "未设置"
    fallback = summary["fallback_summary"]
    quality = summary["quality_comparison_counts"]
    capability = summary["capability_observations"]
    lower_tasks = ", ".join(summary["tasks_where_router_cost_lower"]) or "无"
    repeat_tasks = ", ".join(summary["repeat_tasks"]) or "无"
    findings = [
        "首轮真实任务成本差异为 Router - Baseline = "
        f"{_fmt(summary['first_attempt_router_vs_baseline_cost_delta'])}；这是首轮实际支出，包含失败执行；不含 task_010 与重复运行。",
        "首轮质量对照："
        f"Router-only={quality['router_passed_baseline_failed']}，"
        f"Baseline-only={quality['router_failed_baseline_passed']}，"
        f"both passed={quality['router_passed_baseline_passed']}，"
        f"both failed={quality['router_failed_baseline_failed']}。",
        f"重复验证任务：{repeat_tasks}；重复验证成本={_fmt(summary['repeat_run_cost'])}。",
    ]
    low_high = "；".join(
        f"{name} observed={item['observed']}, count={item['count']}"
        for name, item in capability.items()
    )
    findings.append(f"JEV 低/高能力观察：{low_high}。")
    lines = [
        "# JEV Model Router Pilot Summary",
        "",
        "## 结论",
        "",
        str(decision),
        "",
        "## 任务",
        "",
        f"{summary['total_tasks']}（真实对比 {summary['real_comparison_tasks']}，受控失败 {summary['controlled_failure_tasks']}）",
        "",
        "## 成本",
        "",
        f"Baseline: {_fmt(summary['baseline_total_cost'])}",
        f"Router: {_fmt(summary['router_total_cost'])}",
        f"差异（Router - Baseline，首轮实际支出，含失败执行）: {_fmt(summary['first_attempt_router_vs_baseline_cost_delta'])}",
        f"成本可比任务（两种模式均 route_status=success）: {', '.join(summary['cost_comparable_tasks']) or '无'}",
        f"Router 成本低于 Baseline 的可比任务: {lower_tasks}",
        "",
        "## 任务完成情况",
        "",
        f"Baseline: {summary['baseline_passed']} / {summary['real_comparison_tasks']}",
        f"Router: {summary['router_passed']} / {summary['real_comparison_tasks']}",
        "",
        "## JEV 合理性",
        "",
        f"reasonable: {summary['jev_reasonable']}",
        f"questionable: {summary['jev_questionable']}",
        f"clearly_unreasonable: {summary['jev_clearly_unreasonable']}",
        f"not_applicable: {summary['jev_not_applicable']}",
        "",
        "## 决策有效性",
        "",
        f"结论: {effectiveness}",
        f"观察: {notes}",
        "- Low 判断是否通常可由低能力模型完成：由 advisor 根据结构化观察判断。",
        "- High 判断是否更常需要高能力模型：由 advisor 根据结构化观察判断。",
        "- 是否存在分类看似合理但选模无帮助的案例：由 advisor 判断。",
        "- 当前证据是否足够：由 advisor 判断。",
        "",
        "## 成本可信度",
        "",
        f"精确成本运行数: {summary['exact_cost_runs']}",
        f"估算成本运行数: {summary['cost_estimated_runs']}",
        f"重复验证精确成本运行数: {summary['repeat_exact_cost_runs']}",
        f"重复验证估算成本运行数: {summary['repeat_cost_estimated_runs']}",
        f"实验验证成本（record-level）: {_fmt(summary['experimental_validation_cost'])}",
        "估算成本未被当作精确账单成本；重复运行成本未混入生产对比。",
        "",
        "## Fallback",
        "",
        f"Case A: {'passed' if fallback['case_a']['passed'] else 'failed'}; route={fallback['case_a']['route_status']}; calls={fallback['case_a']['provider_call_counts']}",
        f"Case B: {'passed' if fallback['case_b']['passed'] else 'failed'}; route={fallback['case_b']['route_status']}; calls={fallback['case_b']['provider_call_counts']}",
        f"fallback_tests_passed: {summary['fallback_tests_passed']}",
        "",
        "## 主要发现",
        "",
    ]
    lines.extend(f"{index}. {finding}" for index, finding in enumerate(findings, 1))
    lines.extend(
        [
            "",
            "## 需要调整",
            "",
            "由 advisor 根据上述证据填写；summary builder 不自动生成调整方案。",
            "",
            "## 是否进入真实 Agent 集成",
            "",
            integration_text,
            "",
        ]
    )
    return "\n".join(lines)


def _load_inputs(config_path: Path, results_path: Optional[Path], model_registry_path: Path):
    config_path = Path(config_path)
    try:
        config = load_pilot_config(config_path, model_registry_path=model_registry_path)
    except (PilotConfigError, OSError, ValueError) as exc:
        raise PilotSummaryError("pilot config is not loadable") from exc
    root = config_path.expanduser().resolve(strict=False).parents[1]
    results_root = (root / "benchmark" / "results").resolve(strict=False)
    official_path = _contained(root / config.outputs.official_results_path, results_root, "official results")
    json_path = _contained(root / config.outputs.summary_json_path, results_root, "summary JSON")
    markdown_path = _contained(root / config.outputs.summary_markdown_path, results_root, "summary Markdown")
    if results_path is not None and Path(results_path).expanduser().resolve(strict=False) != official_path:
        raise PilotSummaryError("results path does not match frozen config")
    return config, root, official_path, json_path, markdown_path


def _contained(path: Path, root: Path, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PilotSummaryError(f"{label} path escapes benchmark/results") from exc
    return resolved


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PilotSummaryError("official results are unreadable") from exc
    if not lines:
        raise PilotSummaryError("official results are empty")
    records: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise PilotSummaryError(f"official results contain a blank line at {line_number}")
        try:
            value = json.loads(
                line,
                parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                object_pairs_hook=_object_without_duplicate_keys,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PilotSummaryError(f"official result line {line_number} is malformed JSON") from exc
        if not isinstance(value, dict):
            raise PilotSummaryError(f"official result line {line_number} is not an object")
        records.append(value)
    return records


def _object_without_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validate_records(records: Sequence[Mapping[str, Any]], config: PilotConfig):
    known = set(config.real_task_ids) | {config.controlled_failure_task_id}
    grouped: Dict[Tuple[str, int], Dict[str, Mapping[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping) or record.get("official_pilot") is not True:
            raise PilotSummaryError("official results contain mixed or non-official records")
        task_id = record.get("task_id")
        mode = record.get("mode")
        attempt = record.get("attempt")
        if task_id not in known:
            raise PilotSummaryError("official results contain an unknown task id")
        if mode not in ("baseline", "router"):
            raise PilotSummaryError("official results contain an invalid mode")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise PilotSummaryError("official results contain an invalid attempt")
        key = (task_id, attempt)
        if mode in grouped.setdefault(key, {}):
            raise PilotSummaryError("duplicate (task, attempt, mode) official record")
        if record.get("config_schema_version") not in (None, config.schema_version):
            raise PilotSummaryError("official result config schema does not match frozen config")
        _validate_record_cost(record)
        _validate_acceptance(record, task_id == config.controlled_failure_task_id)
        grouped[key][mode] = record

    expected_tasks = (*config.real_task_ids, config.controlled_failure_task_id)
    if set(task_id for task_id, _attempt in grouped) != set(expected_tasks):
        raise PilotSummaryError("official results do not contain exactly the configured task slots")
    for task_id in expected_tasks:
        attempts = sorted(attempt for current_task, attempt in grouped if current_task == task_id)
        if attempts[0] != 1 or attempts != list(range(1, attempts[-1] + 1)):
            raise PilotSummaryError("repeat attempts are not contiguous")
        if task_id == config.controlled_failure_task_id and attempts != [1]:
            raise PilotSummaryError("controlled fallback task cannot have repeat attempts")
        if attempts[-1] > 1 + config.max_extra_runs_per_task:
            raise PilotSummaryError("repeat attempts exceed the frozen maximum")
        for attempt in attempts:
            pair = grouped[(task_id, attempt)]
            if set(pair) != {"baseline", "router"}:
                raise PilotSummaryError("every official attempt must contain a baseline/router pair")
            _validate_pair_resolution(pair, config, task_id == config.controlled_failure_task_id)
    if len(grouped) < config.total_task_slots:
        raise PilotSummaryError("not all configured Pilot slots have a complete first attempt")
    if len(grouped) - config.total_task_slots > len(config.real_task_ids) * config.max_extra_runs_per_task:
        raise PilotSummaryError("official results contain too many repeat pairs")
    _validate_fallback(grouped[(config.controlled_failure_task_id, 1)])
    return grouped


def _validate_record_cost(record: Mapping[str, Any]) -> None:
    cost = record.get("cost")
    if not isinstance(cost, Mapping):
        raise PilotSummaryError("official record cost is missing")
    _number(cost.get("total_production_cost"), "total_production_cost")
    estimated = cost.get("cost_estimated")
    if not isinstance(estimated, bool):
        raise PilotSummaryError("official record cost_estimated must be boolean")
    if "experimental_validation_cost" in record and record["experimental_validation_cost"] is not None:
        _number(record["experimental_validation_cost"], "experimental_validation_cost")


def _validate_acceptance(record: Mapping[str, Any], controlled: bool) -> None:
    value = record.get("acceptance_status")
    if not isinstance(value, str):
        raise PilotSummaryError("official record acceptance status is missing")
    if value == PENDING_ACCEPTANCE:
        raise PilotSummaryError("manual acceptance is pending")
    allowed = {CONTROLLED_ACCEPTANCE_STATUS} if controlled else set(REAL_ACCEPTANCE_STATUSES)
    if value not in allowed:
        raise PilotSummaryError("official record acceptance status is unresolved or invalid")


def _validate_pair_resolution(pair: Mapping[str, Mapping[str, Any]], config: PilotConfig, controlled: bool) -> None:
    for mode, record in pair.items():
        audit = record.get("jev_audit")
        if not isinstance(audit, Mapping):
            raise PilotSummaryError("official record JEV audit is missing")
        assessment_status = audit.get("assessment_status")
        if assessment_status == PENDING_AUDIT:
            raise PilotSummaryError("JEV audit review is pending")
        if assessment_status not in ("reviewed", "not_applicable"):
            raise PilotSummaryError("official record JEV audit is unresolved")
        assessment = audit.get("assessment")
        if controlled:
            if assessment_status != "not_applicable" or assessment != "not_applicable":
                raise PilotSummaryError("controlled fallback JEV audit must be not_applicable")
        elif mode == "baseline":
            if assessment_status != "not_applicable":
                raise PilotSummaryError("baseline JEV audit must be not_applicable")
        elif assessment_status == "reviewed" and assessment not in AUDIT_ASSESSMENTS - {"not_applicable"}:
            raise PilotSummaryError("reviewed router JEV audit has no valid assessment")
        elif assessment_status == "not_applicable" and assessment not in (None, "not_applicable"):
            raise PilotSummaryError("not_applicable router JEV audit has an invalid assessment")


def _validate_fallback(pair: Mapping[str, Mapping[str, Any]]) -> None:
    records = list(pair.values())
    by_case = {record.get("controlled_mock_case"): record for record in records}
    if set(by_case) != {"case_a_fallback_success", "case_b_budget_block"}:
        raise PilotSummaryError("controlled fallback must contain exactly cases A and B")
    case_a = by_case["case_a_fallback_success"]
    case_b = by_case["case_b_budget_block"]
    for record in records:
        if record.get("acceptance_status") != CONTROLLED_ACCEPTANCE_STATUS:
            raise PilotSummaryError("controlled fallback acceptance is not passed")
        if not isinstance(record.get("controlled_mock_summary"), Mapping):
            raise PilotSummaryError("controlled fallback summary is missing")
    calls_a = _fallback_calls(case_a)
    if case_a.get("route_status") != "success" or case_a.get("route_source") != "fallback":
        raise PilotSummaryError("fallback Case A route contract failed")
    fallback_cost = _number(case_a["cost"].get("fallback_cost"), "fallback_cost")
    if fallback_cost <= 0 or calls_a.get("primary") != 1 or calls_a.get("fallback_1") != 1:
        raise PilotSummaryError("fallback Case A call or cost contract failed")
    calls_b = _fallback_calls(case_b)
    if case_b.get("route_status") != "failed":
        raise PilotSummaryError("fallback Case B route contract failed")
    error_codes = case_b.get("error_codes")
    if not isinstance(error_codes, list) or "budget_limit_reached" not in error_codes:
        raise PilotSummaryError("fallback Case B budget error is missing")
    if calls_b.get("fallback_1") != 0 or calls_b.get("fallback_2") != 0:
        raise PilotSummaryError("fallback Case B must not call fallback models")


def _fallback_calls(record: Mapping[str, Any]) -> Mapping[str, Any]:
    summary = record["controlled_mock_summary"]
    calls = summary.get("provider_call_counts")
    if not isinstance(calls, Mapping):
        raise PilotSummaryError("controlled fallback call counts are missing")
    return calls


def _aggregate(config: PilotConfig, grouped, records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    first = {task: grouped[(task, 1)] for task in config.real_task_ids}
    baseline_records = [first[task]["baseline"] for task in config.real_task_ids]
    router_records = [first[task]["router"] for task in config.real_task_ids]
    baseline_total = _sum_cost(baseline_records)
    router_total = _sum_cost(router_records)
    first_costs = [*baseline_records, *router_records]
    repeats = [
        (task, attempt, grouped[(task, attempt)])
        for task in config.real_task_ids
        for attempt in sorted(attempt_number for current_task, attempt_number in grouped if current_task == task and attempt_number > 1)
    ]
    repeat_records = [record for _task, _attempt, pair in repeats for record in pair.values()]
    repeat_tasks = [task for task in config.real_task_ids if any(current_task == task and attempt > 1 for current_task, attempt in grouped)]
    repeat_evidence = OrderedDict()
    for task in repeat_tasks:
        attempts = sorted(attempt for current_task, attempt in grouped if current_task == task and attempt > 1)
        final_pair = grouped[(task, attempts[-1])]
        repeat_evidence[task] = {
            "attempt_numbers": attempts,
            "final_attempt": attempts[-1],
            "final_acceptance": {mode: final_pair[mode]["acceptance_status"] for mode in ("baseline", "router")},
            "jev_assessment_counts": dict(
                sorted(Counter(
                    pair["router"].get("jev_audit", {}).get("assessment")
                    for current_task, attempt_number, pair in repeats
                    if current_task == task and isinstance(pair["router"].get("jev_audit", {}).get("assessment"), str)
                ).items())
            ),
        }
    quality_tasks = OrderedDict(
        (
            name,
            [
                task
                for task in config.real_task_ids
                if _is_pass(first[task]["router"]) == router_passed
                and _is_pass(first[task]["baseline"]) == baseline_passed
            ],
        )
        for name, router_passed, baseline_passed in (
            ("router_passed_baseline_passed", True, True),
            ("router_passed_baseline_failed", True, False),
            ("router_failed_baseline_passed", False, True),
            ("router_failed_baseline_failed", False, False),
        )
    )
    quality_counts = {name: len(tasks) for name, tasks in quality_tasks.items()}
    choices = {
        mode: dict(sorted(Counter(
            record.get("selected_model")
            for record in (router_records if mode == "router" else baseline_records)
            if isinstance(record.get("selected_model"), str) and record.get("selected_model")
        ).items()))
        for mode in ("baseline", "router")
    }
    router_failures = [
        {"task_id": task, "error_codes": _safe_error_codes(first[task]["router"].get("error_codes"))}
        for task in config.real_task_ids
        if not _is_pass(first[task]["router"]) or first[task]["router"].get("route_status") == "failed"
    ]
    cost_comparable_tasks = [
        task
        for task in config.real_task_ids
        if first[task]["baseline"].get("route_status") == "success"
        and first[task]["router"].get("route_status") == "success"
    ]
    lower_tasks = [
        task
        for task in cost_comparable_tasks
        if _production_cost(first[task]["router"]) < _production_cost(first[task]["baseline"])
    ]
    jev_counts = Counter(
        router.get("jev_audit", {}).get("assessment")
        for router in router_records
        if router.get("jev_audit", {}).get("assessment") in AUDIT_ASSESSMENTS
    )
    capability = OrderedDict()
    for name in ("low", "high"):
        observed = [
            (task, router)
            for task, router in zip(config.real_task_ids, router_records)
            if isinstance(router.get("jev_audit", {}).get("classifier"), Mapping)
            and router["jev_audit"]["classifier"].get("required_capability") == name
        ]
        capability[name] = {
            "observed": bool(observed),
            "count": len(observed),
            "task_ids": [task for task, _router in observed],
            "selected_model_counts": dict(sorted(Counter(
                router.get("selected_model") for _task, router in observed if isinstance(router.get("selected_model"), str)
            ).items())),
            "router_passed": sum(1 for _task, router in observed if _is_pass(router)),
            "router_failed": sum(1 for _task, router in observed if not _is_pass(router)),
        }
    fallback = _fallback_projection(grouped[(config.controlled_failure_task_id, 1)])
    repeat_run_cost = _sum_cost(repeat_records)
    return {
        "schema_version": config.schema_version,
        "workflow": "PILOT_SUMMARY_GATE",
        "workflow_gate": "pilot_summary_required",
        "total_tasks": config.total_task_slots,
        "real_comparison_tasks": len(config.real_task_ids),
        "controlled_failure_tasks": 1,
        "baseline_total_cost": baseline_total,
        "router_total_cost": router_total,
        "baseline_passed": sum(1 for record in baseline_records if _is_pass(record)),
        "router_passed": sum(1 for record in router_records if _is_pass(record)),
        "fallback_tests_passed": fallback["passed"],
        "cost_estimated_runs": sum(1 for record in first_costs if record["cost"]["cost_estimated"]),
        "exact_cost_runs": sum(1 for record in first_costs if not record["cost"]["cost_estimated"]),
        "repeat_cost_estimated_runs": sum(1 for record in repeat_records if record["cost"]["cost_estimated"]),
        "repeat_exact_cost_runs": sum(1 for record in repeat_records if not record["cost"]["cost_estimated"]),
        "experimental_validation_cost": _sum_optional_validation_cost(records),
        "repeat_run_cost": repeat_run_cost,
        "repeat_attempt_pairs": len(repeats),
        "repeat_tasks": repeat_tasks,
        "repeat_evidence": repeat_evidence,
        "jev_reasonable": jev_counts["reasonable"],
        "jev_questionable": jev_counts["questionable"],
        "jev_clearly_unreasonable": jev_counts["clearly_unreasonable"],
        "jev_not_applicable": jev_counts["not_applicable"],
        "first_attempt_router_failures": router_failures,
        "first_attempt_model_choices": choices,
        "first_attempt_router_vs_baseline_cost_delta": _round_cost(router_total - baseline_total),
        "cost_comparable_tasks": cost_comparable_tasks,
        "tasks_where_router_cost_lower": lower_tasks,
        "tasks_where_router_passed_vs_baseline_passed": quality_tasks,
        "quality_comparison_counts": quality_counts,
        "capability_observations": capability,
        "fallback_summary": fallback,
        "decision": None,
        "decision_effectiveness": None,
        "decision_effectiveness_notes": None,
    }


def _fallback_projection(pair: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    by_case = {record["controlled_mock_case"]: record for record in pair.values()}
    result = {"passed": True}
    for name in ("case_a_fallback_success", "case_b_budget_block"):
        record = by_case[name]
        result["case_a" if name.startswith("case_a") else "case_b"] = {
            "passed": True,
            "route_status": record.get("route_status"),
            "route_source": record.get("route_source"),
            "fallback_cost": _production_cost_field(record, "fallback_cost"),
            "provider_call_counts": dict(sorted(_fallback_calls(record).items())),
        }
    return result


def _is_pass(record: Mapping[str, Any]) -> bool:
    return record.get("acceptance_status") in REAL_PASS_STATUSES


def _production_cost(record: Mapping[str, Any]) -> float:
    return _production_cost_field(record, "total_production_cost")


def _production_cost_field(record: Mapping[str, Any], field: str) -> float:
    return _number(record["cost"].get(field), field)


def _sum_cost(records: Iterable[Mapping[str, Any]]) -> float:
    return _round_cost(sum(_production_cost(record) for record in records))


def _sum_optional_validation_cost(records: Iterable[Mapping[str, Any]]) -> float:
    return _round_cost(sum(
        _number(record["experimental_validation_cost"], "experimental_validation_cost")
        for record in records
        if record.get("experimental_validation_cost") is not None
    ))


def _safe_error_codes(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise PilotSummaryError("router error_codes are malformed")
    return sorted(set(value))


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotSummaryError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise PilotSummaryError(f"{label} must be finite and non-negative")
    return number


def _round_cost(value: float) -> float:
    return round(float(value), 12)


def _fmt(value: Any) -> str:
    return f"{float(value):.12g}"


def _validate_choice(value: Any, choices: Sequence[str], label: str) -> None:
    if not isinstance(value, str) or value not in choices:
        raise PilotSummaryError(f"{label} must be one of: {', '.join(choices)}")


def _validate_notes(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise PilotSummaryError("decision notes must be non-empty and at most 2000 characters")
    if _UNSAFE_TEXT.search(value):
        raise PilotSummaryError("decision notes contain unsafe raw or credential-like text")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _assert_safe_projection(value: Any) -> None:
    if isinstance(value, str):
        if _UNSAFE_TEXT.search(value):
            raise PilotSummaryError("summary projection contains unsafe raw or credential-like text")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _assert_safe_projection(str(key))
            _assert_safe_projection(item)
    elif isinstance(value, list):
        for item in value:
            _assert_safe_projection(item)


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _atomic_write_pair(json_path: Path, markdown_path: Path, json_bytes: bytes, markdown_bytes: bytes) -> None:
    if json_path == markdown_path:
        raise PilotSummaryError("summary output paths must be distinct")
    try:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        old = {path: path.read_bytes() if path.exists() else None for path in (json_path, markdown_path)}
        temp_json = _write_temp(json_path, json_bytes)
        temp_markdown = _write_temp(markdown_path, markdown_bytes)
    except OSError as exc:
        raise PilotSummaryError("unable to stage summary outputs") from exc
    try:
        os.replace(temp_json, json_path)
        os.replace(temp_markdown, markdown_path)
        _fsync_directory(json_path.parent)
    except Exception as exc:
        rollback_error = _rollback_outputs(old, json_path, markdown_path, json_bytes, markdown_bytes)
        if rollback_error is not None:
            raise PilotSummaryError("summary replacement failed and rollback failed") from rollback_error
        raise PilotSummaryError("summary replacement failed; outputs rolled back") from exc
    finally:
        for path in (temp_json, temp_markdown):
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _write_temp(target: Path, data: bytes) -> Path:
    fd, raw_path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    path = Path(raw_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return path


def _rollback_outputs(old: Mapping[Path, Optional[bytes]], json_path: Path, markdown_path: Path, json_bytes: bytes, markdown_bytes: bytes) -> Optional[BaseException]:
    try:
        for path, payload in (
            (markdown_path, old[markdown_path]),
            (json_path, old[json_path]),
        ):
            current = path.read_bytes() if path.exists() else None
            if current != payload:
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    restore = _write_temp(path, payload)
                    os.replace(restore, path)
    except Exception as exc:
        return exc
    return None


def _fsync_directory(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the offline JEV Model Router Pilot Summary")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="validate and print decision-free JSON")
    modes.add_argument("--finalize", action="store_true", help="write both frozen summary outputs")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--results", type=Path, default=None)
    parser.add_argument("--model-registry", type=Path, default=DEFAULT_MODEL_REGISTRY_PATH)
    parser.add_argument("--decision", choices=SUMMARY_DECISIONS)
    parser.add_argument("--decision-effectiveness", choices=DECISION_EFFECTIVENESS)
    notes = parser.add_mutually_exclusive_group()
    notes.add_argument("--decision-notes")
    notes.add_argument("--decision-notes-file", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.check:
            if any(value is not None for value in (args.decision, args.decision_effectiveness, args.decision_notes, args.decision_notes_file)):
                raise PilotSummaryError("check mode cannot receive advisor decision fields")
            summary = build_pilot_summary(
                config_path=args.config, results_path=args.results, model_registry_path=args.model_registry
            )
        else:
            if args.decision is None or args.decision_effectiveness is None:
                raise PilotSummaryError("finalization requires decision and decision-effectiveness")
            if (args.decision_notes is None) == (args.decision_notes_file is None):
                raise PilotSummaryError("finalization requires exactly one decision-notes input")
            notes = args.decision_notes
            if args.decision_notes_file is not None:
                try:
                    notes = args.decision_notes_file.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise PilotSummaryError("decision notes file is unreadable") from exc
            summary = finalize_pilot_summary(
                decision=args.decision,
                decision_effectiveness=args.decision_effectiveness,
                decision_notes=notes,
                config_path=args.config,
                results_path=args.results,
                model_registry_path=args.model_registry,
            )
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0
    except PilotSummaryError as exc:
        print(str(exc), file=os.sys.stderr)
        return 2


__all__ = [
    "DECISION_EFFECTIVENESS",
    "PilotSummaryError",
    "SUMMARY_DECISIONS",
    "build_pilot_summary",
    "finalize_pilot_summary",
    "main",
    "render_pilot_summary_markdown",
]
