"""Offline tests for the opt-in PILOT_RUNNER_WIRING bridge."""

import hashlib
import json
from pathlib import Path

import pytest

from benchmark.real_pilot import (
    DEFAULT_REGISTRY_PATH,
    OFFICIAL_RESULT_PATH,
    ControlledMockFixtureError,
    RealPilotConfigurationError,
    RealPilotRuntimeConfig,
    append_jsonl,
    build_real_router,
    persist_pair,
    run_real_pilot_pair,
)
import scripts.real_pilot_gate_smoke as gate_smoke_script
from jev_router.cost import CostComponent
from jev_router.policy import PolicyEngine
from jev_router.providers import ModelResponse
from jev_router.registry import ModelRegistry
from jev_router.schemas import ClassifierResult, TaskType


FIXTURE = Path(__file__).parents[1] / "benchmark" / "fixtures" / "task_002"


def _official_result_snapshot():
    if not OFFICIAL_RESULT_PATH.exists():
        return False, b"", None
    content = OFFICIAL_RESULT_PATH.read_bytes()
    return True, content, hashlib.sha256(content).hexdigest()


def _assert_official_result_unchanged(snapshot):
    existed, content, digest = snapshot
    if not existed:
        assert not OFFICIAL_RESULT_PATH.exists()
        return
    assert OFFICIAL_RESULT_PATH.is_file()
    current = OFFICIAL_RESULT_PATH.read_bytes()
    assert current == content
    assert hashlib.sha256(current).hexdigest() == digest


class FakeJEV:
    def __init__(self, task_type="coding"):
        self.task_type = task_type
        self.calls = []
        self.last_call_metrics = None

    def classify(self, request):
        self.calls.append(request)
        self.last_call_metrics = {
            "returned_model": "jev-test",
            "request_id": "jev-request-test",
            "input_tokens": 10,
            "output_tokens": 2,
            "latency_ms": 1,
            "exact_cost": 0.00042,
            "prompt": "must not persist",
            "Authorization": "Bearer must-not-persist",
        }
        return ClassifierResult(
            task_type=self.task_type,
            difficulty_score=2,
            difficulty_bucket="low",
            required_capability="low",
            confidence=0.9,
            risk_level="low",
        )

    def resolve_cost(self, _request, _classifier):
        return CostComponent(
            cost=0.00042,
            cost_estimated=False,
            cost_estimation_source="provider_usage",
        )


class FakeProvider:
    def __init__(self, name, calls, stack_id):
        self.name = name
        self.calls = calls
        self.stack_id = stack_id
        self.requests = []
        self.last_call_metrics = None

    def invoke(self, request):
        self.calls.append((self.name, request))
        self.requests.append(request)
        workspace = Path(request.metadata["cwd"])
        readme = workspace / "README.md"
        text = readme.read_text(encoding="utf-8")
        readme.write_text(
            text.replace(
                "rg -n 'FILL_BEFORE_PILOT' benchmark/fixtures",
                "grep -R -n 'FILL_BEFORE_PILOT' benchmark/fixtures",
            ),
            encoding="utf-8",
        )
        self.last_call_metrics = {
            "model_id": request.model_id,
            "request_id": f"{self.stack_id}-{self.name}-request",
            "input_tokens": 11,
            "output_tokens": 5,
            "reasoning_tokens": 1,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "provider_reported_cost": 0.01,
            "latency_ms": 2,
            "raw_stdout": "must not persist",
            "prompt": "must not persist",
        }
        return ModelResponse(
            text="raw provider text must not persist",
            input_tokens=11,
            output_tokens=5,
            provider_request_id=f"{self.stack_id}-{self.name}-request",
            provider_reported_cost=0.01,
        )


def runtime(**overrides):
    values = {
        "baseline_model": "low_model",
        "budget_limit": 1.0,
        "estimated_max_costs": {
            "low_model": 0.10,
            "medium_model": 0.25,
            "high_model": 1.00,
        },
    }
    values.update(overrides)
    return RealPilotRuntimeConfig(**values)


def fake_factories(task_type="coding"):
    jevs = []
    provider_stacks = []
    provider_calls = []

    def make_jev():
        jev = FakeJEV(task_type=task_type)
        jevs.append(jev)
        return jev

    def make_provider(name, _definition):
        if not provider_stacks or len(provider_stacks[-1]) == 3:
            provider_stacks.append([])
        provider = FakeProvider(name, provider_calls, len(provider_stacks))
        provider_stacks[-1].append(provider)
        return provider

    return jevs, provider_stacks, make_jev, make_provider, provider_calls


def test_real_pair_uses_runtime_values_without_mutating_fixture_or_running_official_pilot():
    official_snapshot = _official_result_snapshot()
    fixture_task = (FIXTURE / "task.yaml").read_text(encoding="utf-8")
    jevs, provider_stacks, make_jev, make_provider, provider_calls = fake_factories()

    pair = run_real_pilot_pair(
        FIXTURE,
        runtime(),
        jev_factory=make_jev,
        provider_factory=make_provider,
    )

    assert pair["official_pilot"] is False
    assert pair["baseline"]["route_source"] == "manual_override"
    assert pair["baseline"]["selected_model"] == "low_model"
    assert pair["baseline"]["classifier_metrics"] is None
    assert len(jevs) == 1
    assert len(jevs[0].calls) == 1
    assert pair["router"]["classifier"]["task_type"] == "coding"
    assert pair["router"]["route_source"] == "jev"
    assert pair["baseline"]["source_unchanged"] is True
    assert pair["router"]["source_unchanged"] is True
    assert pair["baseline"]["workspace_removed"] is True
    assert pair["router"]["workspace_removed"] is True
    assert (FIXTURE / "task.yaml").read_text(encoding="utf-8") == fixture_task
    assert "FILL_BEFORE_REAL_PILOT" in fixture_task
    _assert_official_result_unchanged(official_snapshot)

    requests = [request for _name, request in provider_calls]
    assert len(requests) == 2
    assert all(request.metadata["auto"] is True for request in requests)
    workspaces = [request.metadata["cwd"] for request in requests]
    assert len(set(workspaces)) == 2
    assert all(not Path(workspace).exists() for workspace in workspaces)
    assert all(request.metadata["cwd"] for request in requests)
    assert len(provider_stacks) == 2
    assert provider_stacks[0] is not provider_stacks[1]
    assert all(
        baseline_provider is not router_provider
        for baseline_provider, router_provider in zip(
            provider_stacks[0], provider_stacks[1]
        )
    )
    assert (
        pair["baseline"]["provider_metrics"]["low_model"]["request_id"]
        != pair["router"]["provider_metrics"][
            pair["router"]["selected_model"]
        ]["request_id"]
    )


def test_sanitized_records_exclude_prompt_context_key_and_raw_provider_output():
    jevs, _provider_stacks, make_jev, make_provider, _calls = fake_factories()
    pair = run_real_pilot_pair(
        FIXTURE,
        runtime(),
        jev_factory=make_jev,
        provider_factory=make_provider,
    )
    encoded = json.dumps(
        {"baseline": pair["baseline"], "router": pair["router"]},
        ensure_ascii=False,
        sort_keys=True,
    )
    for secret in (
        "must not persist",
        "raw provider text must not persist",
        "Authorization",
        "stdout",
        "stderr",
    ):
        assert secret not in encoded
    assert set(pair["router"]) >= {
        "task_id",
        "mode",
        "status",
        "route_source",
        "selected_model",
        "classifier",
        "fallback_history",
        "cost",
        "error_codes",
        "provider_metrics",
        "acceptance_status",
        "source_unchanged",
        "workspace_removed",
    }
    assert (
        pair["router"]["provider_metrics"][pair["router"]["selected_model"]][
            "provider_reported_cost"
        ]
        == 0.01
    )
    assert jevs[0].last_call_metrics["prompt"] == "must not persist"


def test_real_pair_keeps_private_normalized_evidence_separate_from_safe_records():
    jevs, _provider_stacks, make_jev, make_provider, _calls = fake_factories()
    pair = run_real_pilot_pair(
        FIXTURE,
        runtime(),
        jev_factory=make_jev,
        provider_factory=make_provider,
    )

    assert pair["_evidence"]["baseline"]["response_text"] == "raw provider text must not persist"
    assert pair["_evidence"]["router"]["response_text"] == "raw provider text must not persist"
    assert pair["_evidence"]["baseline"]["allowed_paths"]["README.md"]["status"] == "present"
    assert "raw_stdout" not in json.dumps(pair["_evidence"], ensure_ascii=False)
    assert "prompt" not in json.dumps(pair["_evidence"], ensure_ascii=False)


def test_real_pilot_file_operation_classifier_selects_low_model():
    registry = ModelRegistry.from_yaml(DEFAULT_REGISTRY_PATH)
    classifier = ClassifierResult(
        task_type=TaskType.FILE_OPERATION,
        difficulty_score=1,
        difficulty_bucket="low",
        required_capability="low",
        confidence=0.93,
        risk_level="low",
    )

    assert all(
        TaskType.FILE_OPERATION in registry.get(name).task_types
        for name in ("low_model", "medium_model", "high_model")
    )
    decision = PolicyEngine(
        ModelRegistry({"low_model": registry.get("low_model")})
    ).decide(classifier)
    assert decision.primary_model == "low_model"


def test_router_failure_has_no_baseline_provider_metrics(tmp_path):
    registry_path = tmp_path / "models.real-pilot.yaml"
    registry_path.write_text(
        """models:
  low_model:
    provider: opencode_go
    model_id: opencode-go/low
    capability: low
    task_types:
      - coding
    input_cost_per_million: 0.15
    output_cost_per_million: 0.50
    enabled: true
  medium_model:
    provider: opencode_go
    model_id: opencode-go/medium
    capability: medium
    task_types:
      - coding
    input_cost_per_million: 0.15
    output_cost_per_million: 0.47
    enabled: true
  high_model:
    provider: opencode_go
    model_id: opencode-go/high
    capability: high
    task_types:
      - coding
    input_cost_per_million: 0.20
    output_cost_per_million: 1.20
    enabled: true
""",
        encoding="utf-8",
    )
    jevs, provider_stacks, make_jev, make_provider, _calls = fake_factories(
        task_type="file_operation"
    )

    pair = run_real_pilot_pair(
        FIXTURE,
        runtime(registry_path=registry_path),
        jev_factory=make_jev,
        provider_factory=make_provider,
    )

    assert pair["baseline"]["route_source"] == "manual_override"
    assert pair["baseline"]["provider_metrics"]["low_model"]
    assert pair["router"]["route_status"] == "failed"
    assert pair["router"]["route_source"] == "jev"
    assert "no_eligible_model" in pair["router"]["error_codes"]
    assert pair["router"]["selected_model"] is None
    assert pair["router"]["provider_metrics"] == {}
    assert len(jevs) == 1
    assert len(jevs[0].calls) == 1
    assert all(not provider.requests for provider in provider_stacks[1])


def test_runtime_config_validates_finite_non_negative_cost_estimates():
    with pytest.raises(RealPilotConfigurationError):
        runtime(estimated_max_costs={"low_model": float("nan")})
    with pytest.raises(RealPilotConfigurationError):
        runtime(estimated_max_costs={"low_model": -0.1})
    with pytest.raises(RealPilotConfigurationError):
        build_real_router(runtime(estimated_max_costs={"low_model": 0.1}))


def test_jsonl_append_is_one_line_and_rejects_non_finite_values(tmp_path):
    path = tmp_path / "results.jsonl"
    append_jsonl(path, {"official_pilot": False, "value": 1.25})
    assert path.read_text(encoding="utf-8").count("\n") == 1
    assert json.loads(path.read_text(encoding="utf-8"))["value"] == 1.25
    with pytest.raises(RealPilotConfigurationError):
        append_jsonl(path, {"value": float("inf")})
    assert path.read_text(encoding="utf-8").count("\n") == 1


def test_gate_persistence_rejects_official_result_path(tmp_path):
    official_snapshot = _official_result_snapshot()
    pair = {
        "official_pilot": False,
        "baseline": {"official_pilot": False, "mode": "baseline"},
        "router": {"official_pilot": False, "mode": "router"},
    }
    with pytest.raises(RealPilotConfigurationError):
        persist_pair(OFFICIAL_RESULT_PATH, pair)
    output = tmp_path / "pilot_runner_gate_smoke.jsonl"
    persist_pair(output, pair)
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2
    _assert_official_result_unchanged(official_snapshot)


def test_task_010_is_refused_before_building_real_providers():
    calls = []
    with pytest.raises(ControlledMockFixtureError):
        run_real_pilot_pair(
            Path(__file__).parents[1] / "benchmark" / "fixtures" / "task_010_fallback",
            runtime(),
            jev_factory=lambda: calls.append("jev"),
            provider_factory=lambda *_args: calls.append("provider"),
        )
    assert calls == []


def test_gate_smoke_missing_jev_env_refuses_without_creating_official_results(
    monkeypatch, tmp_path, capsys
):
    official_snapshot = _official_result_snapshot()
    monkeypatch.delenv("JEV_API_KEY_FILE", raising=False)
    output = tmp_path / "smoke.jsonl"
    exit_code = gate_smoke_script.main(
        [
            str(FIXTURE),
            "--baseline-model",
            "low_model",
            "--budget-limit",
            "1.0",
            "--max-cost",
            "low_model=0.1",
            "--max-cost",
            "medium_model=0.25",
            "--max-cost",
            "high_model=1.0",
            "--output",
            str(output),
        ]
    )
    assert exit_code == 2
    assert "jev_api_key_file_required" in capsys.readouterr().out
    assert not output.exists()
    _assert_official_result_unchanged(official_snapshot)


@pytest.mark.parametrize("key_file_kind", ["missing", "empty", "directory"])
def test_gate_smoke_configuration_error_is_sanitized_and_writes_nothing(
    monkeypatch, tmp_path, capsys, key_file_kind
):
    key_file = tmp_path / "jev-key"
    if key_file_kind == "empty":
        key_file.write_text("", encoding="utf-8")
    elif key_file_kind == "directory":
        key_file.mkdir()
    monkeypatch.setenv("JEV_API_KEY_FILE", str(key_file))
    monkeypatch.setattr(gate_smoke_script, "PROJECT_ROOT", tmp_path)
    output = tmp_path / "smoke.jsonl"

    exit_code = gate_smoke_script.main(
        [
            str(FIXTURE),
            "--baseline-model",
            "low_model",
            "--budget-limit",
            "1.0",
            "--max-cost",
            "low_model=0.1",
            "--max-cost",
            "medium_model=0.25",
            "--max-cost",
            "high_model=1.0",
            "--output",
            str(output),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "Traceback" not in captured.out
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "official_pilot": False,
        "status": "refused",
        "error_codes": ["jev_configuration_error"],
    }
    assert not output.exists()
