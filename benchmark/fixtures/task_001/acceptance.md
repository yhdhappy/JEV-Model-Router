# Pre-run acceptance criteria

These are checks for the real Pilot run. This fixture records criteria only; it
does not record a result.

- [ ] The route originates from `light_rule`.
- [ ] JEV is not called.
- [ ] The selected model is a low-capability model eligible under Router policy.
- [ ] `README.md` remains byte-for-byte unchanged from `initial_state/README.md`.
- [ ] The returned task output shows evidence that `README.md` was read. Manual
      result inspection is required because the current harness does not expose
      the model response to an acceptance command.
- [ ] No unrelated file changes occur.
