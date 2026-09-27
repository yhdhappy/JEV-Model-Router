# Acceptance criteria

- [ ] Manual evidence shows a new redaction-matrix test was added for
      case-insensitive Authorization/Bearer, all requested key spellings,
      punctuation/quote/newline adjacency, and `sk-*` identifiers.
- [ ] The new matrix tests and focused logging tests pass with project
      `.venv/bin/python -m pytest tests/test_logging_store.py`.
- [ ] Normal task/model IDs and numeric cost fields remain unchanged in the
      logged JSONL record.
- [ ] The output remains one valid JSONL record per append and contains no raw
      secret-like value or request content.
- [ ] Production code changed only if a new test exposed an actual leak; no
      fabricated result, score, model, price, or Pilot decision is recorded.
