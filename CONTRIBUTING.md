# Contributing to zing

Thanks for helping make relay auditing more trustworthy. zing is a black-box
auditing aid: correctness and **not falsely accusing honest relays** matter more
than catching every possible trick. Keep that bar in mind for any change.

## Ground rules

- **Evidence over accusation.** Findings report *divergence and risk*, never
  "fraud." Prefer `INCONCLUSIVE` to a guess. A new HIGH-severity path needs hard,
  reproducible evidence and should be hard to trip on an honest endpoint.
- **No network in tests.** Detector tests run against the in-process mock server
  in `tests/conftest.py` (httpx `MockTransport`), never a live API.
- **Secrets never leave.** API keys are fingerprinted, never stored. Any new
  output path must route relay-controlled text through `zing.utils.redact`.

## Development setup

Requires Python 3.10+.

```bash
pip install -e '.[dev]'      # editable install with dev tools
pytest                       # run the test suite
ruff check zing tests        # lint
mypy zing                    # type-check
```

## Adding a detector

Each detector is a single self-contained file in `zing/detectors/`:

1. Subclass `Detector`, set `id`, `name`, `dimension`, and `min_suite`.
2. Implement `async def run(self, ctx) -> DetectorResult`, returning `Finding`s
   with a status, severity, summary, and `evidence`.
3. Decorate the class with `@register` — the runner discovers it automatically.
4. Add a behavioral test exercising both the flagged and the clean path.

A detector that needs a trusted judge sets `requires_judge = True`; one that needs
a baseline sets `requires_baseline = True`.

## Editing the knowledge base

Model profiles live in `zing/knowledge/data/<provider>.yaml`, one file per
provider. Each model carries its native context window, max output, knowledge
cutoff, tokenizer, capability flags, and identity keywords. When you change a
numeric field, **cite an authoritative source** (the provider's official model
card / pricing / docs) in the PR — a wrong KB value causes false positives against
honest relays.

## Pull requests

- Keep `pytest`, `ruff check`, and `mypy zing` green (CI runs all three on
  Python 3.10–3.13).
- Describe the relay trick or false-positive a change addresses.
- Update `CHANGELOG.md` under `[Unreleased]`.

By contributing you agree your contributions are licensed under the project's
[Apache-2.0](LICENSE) license.
