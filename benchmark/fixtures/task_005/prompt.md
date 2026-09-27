Strengthen `JsonlLoggingStore` redaction tests in `tests/test_logging_store.py`.
Cover mixed-case Authorization/Bearer markers; the `api_key`, `api-key`, and
`apikey` spellings; `access_token`, `password`, and `secret`; quoted,
punctuation-adjacent, and newline-adjacent values; and `sk-*` identifiers.

Prove that normal IDs and numeric costs remain unchanged. Prefer a tests-only
change. Modify `src/jev_router/logging_store.py` only if a new regression test
demonstrates a real leak, and then make the smallest production fix. Do not
weaken the redaction contract, add dependencies, use credentials, or include
real secret values. Work test-first and run the focused acceptance command.
