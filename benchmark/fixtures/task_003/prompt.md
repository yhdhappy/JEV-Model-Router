Task: replace the raw `KeyError` raised by `ModelRegistry.get("missing_model")`
with a stable domain error consistent with `src/jev_router/errors.py`.

Keep the known-model lookup behavior unchanged. Add regression coverage for the
missing-model path and the unchanged known-model path. Work test-first: retain
the reproduced RED case, make the smallest implementation change, then run the
focused acceptance command.

Modify only `src/jev_router/registry.py`, `src/jev_router/errors.py`, and
`tests/test_registry.py`. Do not modify `schemas.py`, `pyproject.toml`, config,
fixture files, or any other path. Do not use network, credentials, paid Pilot
capacity, or real provider calls.
