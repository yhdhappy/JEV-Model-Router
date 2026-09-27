"""T-13 task_010 controlled-failure tests at the final Router contract."""

import json
import os
import socket
import urllib.request

import pytest

from benchmark import failure_injection
from benchmark.failure_injection import (
    FailureInjectionCase,
    UnsupportedFailureCase,
    run_failure_injection,
)


def test_case_a_uses_router_fallback_success_and_accounts_for_real_cost():
    run = run_failure_injection(FailureInjectionCase.FALLBACK_SUCCESS)
    result = run.result

    assert result.status == "success"
    assert result.route_source == "fallback"
    assert result.selected_model == "fallback_1"
    assert result.fallback_history == [
        "primary:model_unavailable",
        "fallback_1:success",
    ]
    assert run.fallback_reason == "model_unavailable"
    assert result.errors[0].code == "model_unavailable"
    assert run.provider_call_counts == {
        "primary": 1,
        "fallback_1": 1,
    }
    assert run.primary.requests[0].model_id == "actual-primary"
    assert run.fallback_1.requests[0].model_id == "actual-fallback-1"
    assert result.cost.fallback_cost > 0
    assert result.cost.execution_cost == 0
    assert result.cost.classifier_cost > 0
    assert result.cost.total_production_cost == pytest.approx(
        result.cost.classifier_cost
        + result.cost.execution_cost
        + result.cost.fallback_cost
    )
    assert result.cost.cost_estimated is False
    assert "Provider model unavailable" not in run.to_summary_json()
    assert "sk-do-not-return" not in run.to_summary_json()


def test_case_b_budget_blocks_before_fallback_and_more_expensive_candidate():
    run = run_failure_injection(FailureInjectionCase.BUDGET_BLOCK)
    result = run.result

    assert result.status == "failed"
    assert result.errors[-1].code == "budget_limit_reached"
    assert result.fallback_history == [
        "primary:provider_timeout",
        "fallback_1:budget_limit_reached",
    ]
    assert run.fallback_reason == "budget_limit_reached"
    assert run.provider_call_counts == {
        "primary": 1,
        "fallback_1": 0,
        "fallback_2": 0,
    }
    assert result.cost.classifier_cost == pytest.approx(0.04)
    assert result.cost.execution_cost == 0
    assert result.cost.fallback_cost == 0
    assert result.cost.total_production_cost == pytest.approx(
        result.cost.classifier_cost
    )
    assert result.cost.cost_estimated is False
    assert "retry" not in run.to_summary_json().lower()


@pytest.mark.parametrize(
    "case",
    [FailureInjectionCase.FALLBACK_SUCCESS, FailureInjectionCase.BUDGET_BLOCK],
)
def test_repeated_runs_are_semantically_equivalent_with_fresh_call_counts(case):
    first = run_failure_injection(case)
    second = run_failure_injection(case)

    assert first.to_summary() == second.to_summary()
    assert first.provider_call_counts == second.provider_call_counts
    assert first.primary is not second.primary
    assert first.fallback_1 is not second.fallback_1


def test_unsupported_case_fails_closed_without_router_or_provider_calls(monkeypatch):
    route_calls = []
    provider_calls = []

    def unexpected_route(*args, **kwargs):
        route_calls.append((args, kwargs))
        raise AssertionError("unsupported case must not route")

    monkeypatch.setattr(failure_injection.Router, "route", unexpected_route)
    monkeypatch.setattr(
        failure_injection.MockProvider,
        "invoke",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)),
    )

    with pytest.raises(UnsupportedFailureCase) as exc_info:
        run_failure_injection("case_010_not_documented")

    assert exc_info.value.code == "unsupported_failure_case"
    assert str(exc_info.value) == "unsupported_failure_case: unsupported case"
    assert route_calls == []
    assert provider_calls == []


@pytest.mark.parametrize(
    "case",
    [FailureInjectionCase.FALLBACK_SUCCESS, FailureInjectionCase.BUDGET_BLOCK],
)
def test_exported_summary_is_json_safe_and_excludes_provider_response(case):
    run = run_failure_injection(case)

    encoded = run.to_summary_json()
    decoded = json.loads(encoded)

    assert decoded == run.to_summary()
    assert "provider_response" not in decoded
    assert "sk-do-not-return" not in encoded
    assert "raw-provider-detail" not in encoded


def test_task_010_uses_no_network_or_credential_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network or credential access is forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)

    run = run_failure_injection(FailureInjectionCase.FALLBACK_SUCCESS)

    assert run.result.status == "success"
