Use the existing `benchmark.failure_injection.run_failure_injection` seam for
both documented cases: `case_a_fallback_success` and `case_b_budget_block`.
Create only `failure_injection_report.json` with safe summaries.

The report must prove Case A success through a real fallback, a positive
fallback cost, and the expected provider call counts. It must prove Case B
failed with `budget_limit_reached` and zero calls to both fallback providers.
Do not duplicate fallback logic, add network/provider configuration, expose
prompts or raw provider responses, include credentials/secrets, or claim real-
provider success. Label the report explicitly as a controlled Mock task.
