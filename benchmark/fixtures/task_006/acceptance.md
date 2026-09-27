# Acceptance criteria

- [ ] In the real editable environment, `.venv/bin/pytest -q
      tests/test_benchmark.py` collects and passes without
      `ModuleNotFoundError: benchmark`.
- [ ] `python -m pytest -q tests/test_benchmark.py` continues to collect and
      pass.
- [ ] Only `pyproject.toml` and, if the caveat is obsolete, `README.md` may
      change; benchmark files and tests remain unchanged.
- [ ] No Pilot result, score, provider model, price, credential, or network
      evidence is recorded; this fixture is manual because its console-script
      environment is not present in the isolated snapshot.
