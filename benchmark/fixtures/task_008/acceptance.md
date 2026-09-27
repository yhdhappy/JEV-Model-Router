# Acceptance criteria

- [ ] Manual evidence captures the pre-fix behavior where a safe-default result
      can log `fallback_used=true` solely because `fallback_history` is nonempty.
- [ ] Manual evidence captures the post-fix safe-default behavior:
      `route_source=safe_default`, `jev_called=true`, `fallback_used=false`,
      and the full history retained for traceability.
- [ ] Evidence shows an actual primary-to-fallback result keeps
      `route_source=fallback` and `fallback_used=true`, while normal
      non-fallback routes remain unchanged.
- [ ] Focused logging and Router tests pass with project
      `.venv/bin/python -m pytest tests/test_logging_store.py tests/test_router.py`.
- [ ] Only the two paths named in `task.yaml` changed; no fabricated result,
      score, model, price, credential, or Pilot decision is recorded.
