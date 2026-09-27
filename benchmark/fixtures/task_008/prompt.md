Fix the logging metric semantics in `JsonlLoggingStore`: the current
`fallback_used` calculation treats any non-empty `fallback_history` as an
execution fallback, but the safe-default route also writes history.

Make the semantics explicit. A safe-default result must keep
`route_source="safe_default"`, `jev_called=true`, and `fallback_used=false`.
Only a real primary-to-fallback model chain should set `fallback_used=true`;
an actual `route_source="fallback"` remains true. Preserve the full
`fallback_history` so the route remains traceable. Add focused regression tests
for safe-default and real fallback cases, then run the automated acceptance
command.

Modify only `src/jev_router/logging_store.py` and
`tests/test_logging_store.py`. Do not alter Router routing behavior, schemas,
provider code, credentials, network behavior, or any other path.
