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
    execute_official_repeat,
    JEV_AUDIT_REVIEW_CONFIRMATION_TEXT,
    MANUAL_REVIEW_CONFIRMATION_TEXT,
    REPEAT_CONFIRMATION_TEXT,
    OfficialPilotGateError,
    _issue_official_authorization,
    execute_official_task,
    finalize_jev_audit_assessment,
    finalize_manual_acceptance,
    preflight_official_repeat,
    preflight_official_task,
)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.execute and args.confirm != CONFIRMATION_TEXT:
        print(json.dumps(_refusal("confirmation_required"), sort_keys=True, separators=(",", ":")))
        return 2
    if args.repeat and args.confirm != REPEAT_CONFIRMATION_TEXT:
        print(json.dumps(_refusal("repeat_confirmation_required"), sort_keys=True, separators=(",", ":")))
        return 2
    if args.review and args.confirm != MANUAL_REVIEW_CONFIRMATION_TEXT:
        print(json.dumps(_refusal("manual_review_confirmation_required"), sort_keys=True, separators=(",", ":")))
        return 2
    if args.audit_review and args.confirm != JEV_AUDIT_REVIEW_CONFIRMATION_TEXT:
        print(json.dumps(_refusal("jev_audit_review_confirmation_required"), sort_keys=True, separators=(",", ":")))
        return 2
    if args.review and (args.baseline is None or args.router is None):
        print(json.dumps(_refusal("manual_verdict_required"), sort_keys=True, separators=(",", ":")))
        return 2
    if args.audit_review and args.jev_assessment is None:
        print(json.dumps(_refusal("jev_assessment_required"), sort_keys=True, separators=(",", ":")))
        return 2
    try:
        if args.review:
            summary = finalize_manual_acceptance(
                args.task,
                _issue_official_authorization(),
                args.baseline == "pass",
                args.router == "pass",
                **({"attempt": args.attempt} if args.attempt != 1 else {}),
            )
            print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
            return 0
        if args.audit_review:
            summary = finalize_jev_audit_assessment(
                args.task,
                _issue_official_authorization(),
                args.jev_assessment,
                **({"attempt": args.attempt} if args.attempt != 1 else {}),
            )
            print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
            return 0
        if args.repeat:
            trigger_values = args.repeat_triggers or []
            preflight = preflight_official_repeat(
                args.task,
                trigger_values,
                attempt=None if args.attempt == 1 else args.attempt,
            )
            summary = execute_official_repeat(
                args.task,
                _issue_official_authorization(),
                preflight,
            )
            print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
            return 0
        if args.attempt != 1:
            print(json.dumps(_refusal("normal_attempt_invalid"), sort_keys=True, separators=(",", ":")))
            return 2
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
    modes.add_argument("--repeat", action="store_true", help="execute one advisor-authorized repeat pair")
    modes.add_argument("--review", action="store_true", help="finalize advisor manual acceptance verdicts")
    modes.add_argument("--audit-review", action="store_true", help="finalize one router JEV audit assessment")
    parser.add_argument("--confirm", default=None, help="must equal the exact confirmation text for the selected mode")
    parser.add_argument("--attempt", type=int, default=1, help="attempt number for review; repeats derive the next attempt")
    parser.add_argument(
        "--repeat-trigger",
        "--repeat-triggers",
        "--trigger",
        "--triggers",
        dest="repeat_triggers",
        action="append",
        help="repeat trigger name or comma-separated trigger names; may be repeated",
    )
    parser.add_argument("--baseline", choices=("pass", "fail"), help="manual baseline verdict")
    parser.add_argument("--router", choices=("pass", "fail"), help="manual router verdict")
    parser.add_argument(
        "--jev-assessment",
        choices=("reasonable", "questionable", "clearly_unreasonable"),
        help="human assessment for one pending router JEV audit",
    )
    return parser


def _refusal(error_code: str):
    return {"official_pilot": True, "status": "refused", "error_codes": [error_code]}


if __name__ == "__main__":
    raise SystemExit(main())
