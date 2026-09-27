# Acceptance criteria

- [ ] Manual evidence captures the reproduced pre-fix bug: the unavailable
      candidate reports `called=False` while `current_accumulated_cost` becomes
      `0.25` and the attempt cost is `0.25`.
- [ ] Manual evidence captures the post-fix no-call path: accumulated cost is
      unchanged and no actual attempt cost is recorded.
- [ ] Evidence shows the called path still preserves its supplied cost and the
      successful fallback accounting, with provider call counts distinguishing
      no-call from called attempts.
- [ ] The focused regressions pass with project
      `.venv/bin/python -m pytest tests/test_fallback.py`.
- [ ] Only the two paths named in `task.yaml` changed; no real provider,
      credential, prompt, raw response, fabricated price, or Pilot result is
      introduced.
