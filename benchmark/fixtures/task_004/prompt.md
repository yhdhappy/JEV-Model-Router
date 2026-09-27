Reproduce and fix the fallback accounting bug in
`execute_model_fallback(candidates=["missing_model"], providers={},
cost_by_model={"missing_model": 0.25})`: the unavailable/no-call path currently
reports `called=False` but consumes `current_accumulated_cost=0.25` and stores an
attempt cost of `0.25`.

Make the smallest fix so a provider-unavailable candidate consumes no actual
production cost and records no attempt cost. Preserve the existing cost
semantics for an attempt that actually calls a provider. Add regression tests
for both paths and retain the reproduced RED case before implementation.

Modify only `src/jev_router/fallback.py` and `tests/test_fallback.py`. Do not
modify direct dependency snapshots, fixture files, network/provider adapters,
credentials, or any other path.
