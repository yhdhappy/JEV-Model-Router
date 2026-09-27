Reproduce the packaging/import issue described by running
`.venv/bin/pytest -q tests/test_benchmark.py` from the repository root: the
direct pytest console script currently fails to import `benchmark`, while
`python -m pytest` works.

Fix the packaging/import configuration so both the direct pytest console script
and the editable-install/module entry point work. Keep the change minimal and
update the README caveat only if it becomes obsolete. Do not change benchmark
implementation files or tests, do not add a dependency, and do not use network,
credentials, paid Pilot capacity, or fabricated results.

The isolated fixture does not contain the developer's `.venv`, so acceptance is
manual: reproduce in the real editable environment, run both entry points, and
inspect the resulting diff and README claim.
