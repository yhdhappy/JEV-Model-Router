# Pre-run acceptance criteria

These criteria define the real Pilot check; this fixture records no execution
result.

- [ ] Only `README.md` may change; this boundary is encoded by
      `acceptance.allowed_paths`.
- [ ] The old `rg -n 'FILL_BEFORE_PILOT' benchmark/fixtures` command is removed.
- [ ] The new `grep -R -n 'FILL_BEFORE_PILOT' benchmark/fixtures` command is
      present.
- [ ] No README content changes beyond that single command replacement.
- [ ] No fabricated pass, score, or Pilot result is recorded before execution.
