# Acceptance criteria

- [ ] Manual evidence shows `failure_injection_report.json` was created using
      the existing `benchmark.failure_injection.run_failure_injection` seam,
      with no duplicated fallback logic or network/provider configuration.
- [ ] Both documented helper cases were run with the project interpreter:
      `.venv/bin/python`.
- [ ] Case A is successful through `route_source=fallback`, has positive
      fallback cost, and has one primary plus one first-fallback call.
- [ ] Case B is failed with `budget_limit_reached`, and both fallback provider
      call counts are zero.
- [ ] The report contains safe summaries only: no prompt, raw provider
      response, credential, secret-like identifier, or network evidence.
- [ ] The report is labeled as controlled Mock behavior, not real-provider
      success, and only `failure_injection_report.json` changed.
