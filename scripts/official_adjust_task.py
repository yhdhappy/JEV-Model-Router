#!/usr/bin/env python3
"""CLI for the one-task-at-a-time PILOT_ADJUST_EXECUTION_GATE.

With no action flag this command performs only a side-effect-free preflight.
Execution and human reviews require separate opaque confirmations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.pilot_adjust_execution import (  # noqa: E402
    ADJUST_CONFIRMATION_TEXT,
    ADJUST_JEV_REVIEW_CONFIRMATION_TEXT,
    ADJUST_MANUAL_REVIEW_CONFIRMATION_TEXT,
    PilotAdjustExecutionError,
    execute_adjust_task,
    finalize_adjust_jev_audit,
    finalize_adjust_manual_acceptance,
    issue_adjust_authorization,
    preflight_adjust_task,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline preflight / explicitly authorized Adjust gate")
    parser.add_argument("--task", required=True, choices=("task_003", "task_004", "task_005", "task_006", "task_008"))
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--preflight", action="store_true", help="side-effect-free default")
    actions.add_argument("--execute", action="store_true")
    actions.add_argument("--manual-review", choices=("pass", "fail"))
    actions.add_argument("--jev-review", choices=("reasonable", "questionable", "clearly_unreasonable"))
    parser.add_argument("--confirm", default=None)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.execute:
            if args.confirm != ADJUST_CONFIRMATION_TEXT:
                raise PilotAdjustExecutionError(f"confirmation required: {ADJUST_CONFIRMATION_TEXT}", "authorization_required")
            value = execute_adjust_task(args.task, authorization=issue_adjust_authorization())
        elif args.manual_review is not None:
            if args.confirm != ADJUST_MANUAL_REVIEW_CONFIRMATION_TEXT:
                raise PilotAdjustExecutionError(f"confirmation required: {ADJUST_MANUAL_REVIEW_CONFIRMATION_TEXT}", "authorization_required")
            value = finalize_adjust_manual_acceptance(args.task, args.manual_review == "pass", authorization=issue_adjust_authorization())
        elif args.jev_review is not None:
            if args.confirm != ADJUST_JEV_REVIEW_CONFIRMATION_TEXT:
                raise PilotAdjustExecutionError(f"confirmation required: {ADJUST_JEV_REVIEW_CONFIRMATION_TEXT}", "authorization_required")
            value = finalize_adjust_jev_audit(args.task, args.jev_review, authorization=issue_adjust_authorization())
        else:
            value = preflight_adjust_task(args.task)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except PilotAdjustExecutionError as exc:
        print(json.dumps({"status": "blocked", "error_code": exc.error_code, "message": str(exc).split(": ", 1)[-1]}, ensure_ascii=False, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
