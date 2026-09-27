"""Opt-in one-request real provider gate smoke.

This is intentionally not the 10-task Pilot. It makes one JEV call and one
OpenCode call for a non-light-rule debugging request, then prints safe fields
only. The JEV key path must be supplied through ``JEV_API_KEY_FILE``.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from jev_router.opencode_go import OpenCodeGoProvider
from jev_router.real_jev import RealJEVClassifier
from jev_router.registry import ModelRegistry
from jev_router.router import Router
from jev_router.schemas import RouteRequest


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "models.real-pilot.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier",
        choices=("low", "medium", "high"),
        default="medium",
        help="initial smoke tier; does not run the Pilot",
    )
    args = parser.parse_args()

    if not os.environ.get("JEV_API_KEY_FILE", "").strip():
        _print({"status": "skipped", "errors": ["missing_jev_api_key_file"]})
        return 2

    try:
        configured_registry = ModelRegistry.from_yaml(CONFIG_PATH)
        selected_tier = f"{args.tier}_model"
        registry = ModelRegistry(
            {selected_tier: configured_registry.get(selected_tier)}
        )
        jev = RealJEVClassifier()
        providers = {
            name: OpenCodeGoProvider()
            for name in registry.enabled_names()
        }
        router = Router(
            registry,
            providers,
            jev,
            safe_default_model=selected_tier,
            classifier_cost_resolver=jev.resolve_cost,
        )
        request = RouteRequest(
            task_id="real-provider-gate-smoke",
            prompt=(
                "Debug a failing login unit test. Identify the likely cause, "
                "make the smallest safe fix, and report what should be verified."
            ),
            metadata={"smoke_tier": selected_tier},
        )
        result = router.route(request)
        selected_provider = providers.get(result.selected_model or "")
        execution_metrics = (
            selected_provider.last_call_metrics
            if selected_provider is not None
            else None
        )
        _print(
            {
                "status": result.status,
                "route_source": result.route_source,
                "classifier": _classifier_summary(result),
                "selected_model": result.selected_model,
                "jev": jev.last_call_metrics,
                "execution": execution_metrics,
                "execution_cost": result.cost.execution_cost + result.cost.fallback_cost,
                "cost_estimated": result.cost.cost_estimated,
                "cost_source": result.cost.cost_estimation_source,
                "total_cost": result.cost.total_production_cost,
                "errors": [error.code for error in result.errors],
            }
        )
        return 0 if result.status == "success" else 1
    except Exception as exc:
        _print({"status": "failed", "errors": [_safe_error_code(exc)]})
        return 1


def _classifier_summary(result):
    classifier = result.classifier
    if classifier is None:
        return None
    return {
        "task_type": classifier.task_type.value,
        "difficulty_score": classifier.difficulty_score,
        "difficulty_bucket": classifier.difficulty_bucket.value,
        "required_capability": classifier.required_capability.value,
        "confidence": classifier.confidence,
        "risk_level": classifier.risk_level.value,
    }


def _safe_error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else "real_provider_smoke_error"


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    sys.exit(main())
