# Acceptance criteria

- [ ] Manual evidence captures the pre-fix RED reproduction: `get("missing_model")`
      raises the raw `KeyError` before the change.
- [ ] Manual evidence captures the post-fix behavior: the same lookup raises a
      stable domain error consistent with `src/jev_router/errors.py`, without a
      raw lookup exception as the public contract.
- [ ] Evidence shows a known model still returns the same `ModelDefinition` and
      existing registry validation behavior remains unchanged.
- [ ] A new regression test covers both missing and known lookup paths, and it
      passes with project `.venv/bin/python -m pytest tests/test_registry.py`.
- [ ] Only the three paths named in `task.yaml` changed; no result, score,
      provider model, price, credential, or Pilot decision is recorded.
