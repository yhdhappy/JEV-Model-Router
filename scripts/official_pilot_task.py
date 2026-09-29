"""Non-interactive command line entry point for one official Pilot task."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmark.official_pilot import (
    CONFIRMATION_TEXT,
    MANUAL_REVIEW_CONFIRMATION_TEXT,
    OfficialPilotGateError,
    _issue_official_authorization,
    execute_official_task,
    finalize_manual_acceptance,
    preflight_official_task,
)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.execute and args.confirm != CONFIRMATION_TEXT:
        print(json.dumps(_refusal("confirmation_required"), sort_keys=True, separators=(",", ":")))
        return 2
    if args.review and args.confirm != MANUAL_REVIEW_CONFIRMATION_TEXT:
        print(json.dumps(_refusal("manual_review_confirmation_required"), sort_keys=True, separators=(",", ":")))
        return 2
    if args.review and (args.baseline is None or args.router is None):
        print(json.dumps(_refusal("manual_verdict_required"), sort_keys=True, separators=(",", ":")))
        return 2
    try:
        if args.review:
            summary = finalize_manual_acceptance(
                args.task,
                _issue_official_authorization(),
                args.baseline == "pass",
                args.router == "pass",
            )
            print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
            return 0
        preflight = preflight_official_task(args.task)
        if not args.execute:
            print(json.dumps(preflight.sanitized_summary(), sort_keys=True, separators=(",", ":")))
            return 0
        summary = execute_official_task(
            args.task,
            _issue_official_authorization(),
            preflight,
        )
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
        return 0
    except OfficialPilotGateError as exc:
        print(json.dumps(_refusal(exc.error_code), sort_keys=True, separators=(",", ":")))
        return 2
    except (OSError, ValueError):
        print(json.dumps(_refusal("official_pilot_failed"), sort_keys=True, separators=(",", ":")))
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Preflight or execute exactly one official Pilot task")
    parser.add_argument("--task", required=True, help="one exact task_XXX id; ranges and multiple tasks are refused")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true", help="execute after preflight and exact confirmation")
    modes.add_argument("--review", action="store_true", help="finalize advisor manual acceptance verdicts")
    parser.add_argument("--confirm", default=None, help="must equal the exact confirmation text for the selected mode")
    parser.add_argument("--baseline", choices=("pass", "fail"), help="manual baseline verdict")
    parser.add_argument("--router", choices=("pass", "fail"), help="manual router verdict")
    return parser


def _refusal(error_code: str):
    return {"official_pilot": True, "status": "refused", "error_codes": [error_code]}


if __name__ == "__main__":
    raise SystemExit(main())
