"""Run one opt-in real baseline/router pair for PILOT_RUNNER_WIRING only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmark.real_pilot import (
    DEFAULT_GATE_SMOKE_RESULT_PATH,
    RealPilotConfigurationError,
    RealPilotRuntimeConfig,
    ControlledMockFixtureError,
    persist_pair,
    run_real_pilot_pair,
)
from jev_router.errors import ConfigurationError


DEFAULT_FIXTURE = PROJECT_ROOT / "benchmark" / "fixtures" / "task_002"


def main(argv: Optional[List[str]] = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not os.environ.get("JEV_API_KEY_FILE"):
        print(json.dumps({"official_pilot": False, "status": "refused", "error_codes": ["jev_api_key_file_required"]}))
        return 2
    if args.fixture.name == "task_010_fallback":
        print(json.dumps({"official_pilot": False, "status": "refused", "error_codes": [ControlledMockFixtureError.code]}))
        return 2
    try:
        args.output.expanduser().resolve(strict=False).relative_to(PROJECT_ROOT)
    except ValueError:
        print(json.dumps({"official_pilot": False, "status": "refused", "error_codes": ["output_outside_project"]}))
        return 2

    try:
        runtime = RealPilotRuntimeConfig(
            baseline_model=args.baseline_model,
            budget_limit=args.budget_limit,
            estimated_max_costs=_parse_costs(args.max_cost),
        )
        pair = run_real_pilot_pair(args.fixture, runtime)
        persist_pair(args.output, pair)
    except ConfigurationError:
        print(json.dumps({"official_pilot": False, "status": "refused", "error_codes": ["jev_configuration_error"]}))
        return 2
    except (RealPilotConfigurationError, ControlledMockFixtureError, OSError, ValueError):
        print(json.dumps({"official_pilot": False, "status": "failed", "error_codes": ["gate_smoke_failed"]}))
        return 1

    print("OFFICIAL_PILOT=false")
    print(json.dumps(pair, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run exactly one real Pilot runner wiring pair")
    parser.add_argument("fixture", nargs="?", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--baseline-model", required=True)
    parser.add_argument("--budget-limit", required=True, type=float)
    parser.add_argument(
        "--max-cost",
        action="append",
        required=True,
        metavar="MODEL=AMOUNT",
        help="conservative estimate; repeat once per enabled real-pilot model",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_GATE_SMOKE_RESULT_PATH)
    return parser


def _parse_costs(values: List[str]):
    costs = {}
    for item in values:
        name, separator, raw_value = item.partition("=")
        if not separator or not name.strip() or not raw_value.strip():
            raise ValueError("invalid max-cost assignment")
        costs[name.strip()] = float(raw_value)
    return costs


if __name__ == "__main__":
    raise SystemExit(main())
