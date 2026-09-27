Create only `docs/REAL_PROVIDER_PILOT_INTEGRATION_PLAN.md`. Produce an
implementation-ready plan that can clear the
`real_provider_and_pricing_required` gate; do not write network code or use
credentials.

The plan must cover: the provider-adapter boundary; `credential_ref` and
no-secret handling; real model IDs and a cited pricing source/update process;
exact versus estimated usage; the Budget Guard maximum-cost source; stable
error normalization; a placeholder-only configuration example; rollout,
tests, and rollback; and exact conditions for clearing the gate. Keep claims
scoped to this repository's current evidence and label unresolved decisions.

Read the supplied frozen and technical documents plus the current config and
router boundaries before writing. Do not modify any source, test, config,
README, fixture, credential, or other document.
