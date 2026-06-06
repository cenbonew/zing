# zing — LLM relay reality check

> **English** · [中文](README.zh-CN.md)

[![CI](https://github.com/cenbonew/zing/actions/workflows/ci.yml/badge.svg)](https://github.com/cenbonew/zing/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**zing** is a local-first CLI that audits whether an OpenAI-compatible API relay
(中转站 / reseller / proxy) actually serves the model it claims to — or quietly
substitutes a cheaper one, truncates your context window, fakes streaming, or
inflates token billing (**货不对板检测**).

You point it at a relay endpoint and the model it advertises; zing runs a battery
of black-box probes, compares the observed behavior against a bundled knowledge
base of **85 native model profiles across 7 platforms**, and prints a clear,
evidence-backed verdict — for a human, or as JSON for another tool / LLM to read.

> zing reports **black-box evidence of divergence and risk, not cryptographic
> proof of fraud.** See [Responsible use](#responsible-use).

---

## Why

The relay-key market is full of "GPT-4o for 1/10th the price" offers. Many are
honest. Some are not — and the dishonest ones are hard to catch by eye:

- You ask for `gpt-4o`; you're quietly served `gpt-4o-mini` or an open model.
- The relay advertises a 1M-token context but silently truncates to 32K.
- "Streaming" is the full response buffered and re-chunked, with no latency win.
- Reported `usage` tokens are inflated, so your balance burns faster than it should.
- A model that should support tool-calling / JSON mode quietly doesn't.

zing turns "this feels off" into a reproducible report.

## Install

Requires Python 3.10+.

```bash
# from PyPI
pip install zing-audit          # the `zing` command

# or from source
git clone https://github.com/cenbonew/zing
cd zing
pip install -e .
```

(Maintainers: see [docs/PUBLISHING.md](docs/PUBLISHING.md) for the release process.)

Optional: install the `tokenizers` extra for accurate OpenAI-family token
counting in the billing audit:

```bash
pip install -e '.[tokenizers]'
```

## Quick start

```bash
# 1) audit a relay against what it claims (model id + provider hint)
export ZING_API_KEY=sk-your-relay-key
zing check \
  --base-url https://relay.example.com/v1 \
  --api-key env:ZING_API_KEY \
  --model gpt-4o \
  --suite standard

# 2) the strongest check: compare against a trusted baseline of the same model
export OPENAI_API_KEY=sk-your-openai-key
zing compare \
  --target-base-url https://relay.example.com/v1 --target-api-key env:ZING_API_KEY --target-model gpt-4o \
  --baseline-base-url https://api.openai.com/v1 --baseline-api-key env:OPENAI_API_KEY --baseline-model gpt-4o \
  --suite deep

# 3) inspect the bundled knowledge base
zing kb            # all 85 models
zing kb deepseek   # one provider

# 4) generate a config you can commit
zing init          # writes zing.yaml
zing check -c zing.yaml
```

### As a tool for an LLM / agent

`--json` prints the structured report to stdout instead of writing files — feed
it straight to another program or model:

```bash
zing check --base-url ... --model gpt-4o --json | jq .verdict
```

## What it checks

zing scores nine dimensions. The three that most directly reveal 货不对板
(model identity, real context window, capability claims) carry the most weight.

| Dimension | What it catches |
|---|---|
| **model_identity** | Silent model downgrade/substitution — self-identification, knowledge-cutoff, tokenizer fingerprints, the echoed `model` field |
| **context_window** | Silent context truncation (claim 1M, recall fails at 32K) and lost-in-the-middle from cheap RAG/summarization shims, via needle-in-a-haystack + binary search |
| **capability** | Tool-calling / JSON-mode / json-schema / max-output claims that aren't actually delivered (or *over*-delivered, hinting at a substitute) |
| **billing** | Token/usage inflation and missing/unverifiable usage accounting, via an independent tokenizer estimate |
| **streaming** | Fake streaming (buffer-then-chunk) detected from chunk count and inter-chunk timing |
| **protocol** | OpenAI-compatibility conformance: multi-turn, stop sequences, response shape, error schema — and a determinism sub-check for response caching that ignores temperature/seed |
| **reliability** | Concurrent success rate and latency (HTTP 429 throttling bucketed separately) |
| **connectivity** | Endpoint reachability and the advertised `/v1/models` list |
| **security** | Transport (HTTPS), header hygiene, secret echo |

See [docs/METHODOLOGY.md](docs/METHODOLOGY.md) for the technique behind each check,
which relay trick it maps to, and its false-positive caveats.

## Two detection modes

- **Pure code (default):** every deterministic probe — fingerprints, context
  sweep, billing math, streaming timing. No second model needed; fully
  reproducible.
- **Code + LLM hybrid (`--judge`):** additionally consults a *trusted* judge
  model (configured separately, never the target) to assess fuzzy signals like
  quality and reasoning depth that pure code can't decide. Powers the
  `quality_judge` detector.

```bash
zing check --base-url ... --model gpt-4o --suite deep --judge \
  --judge-base-url https://api.openai.com/v1 --judge-api-key env:OPENAI_API_KEY --judge-model gpt-4o-mini
```

## Suites

| Suite | Detectors | Cost |
|---|---|---|
| `smoke` | connectivity, security | very low |
| `standard` | + protocol, model_identity, capability, streaming, billing, reliability | low–medium |
| `deep` | + context_window, determinism, quality_judge (if `--judge`) | higher (long-context probes cost tokens) |
| `full` | everything | highest |

The context-window probe is bounded by `--max-context-tokens` (default 200K) so
auditing a 1M-token model stays affordable.

## Example verdict

```text
╭─ ✗ HIGH RISK — Strong evidence the relay does not deliver the claimed model… ─╮
│ Target : my-relay · model gpt-4o · provider openai                            │
│ Mode   : check · suite deep                                                   │
│ Score  : 53.5/100 (rating F) · confidence medium                             │
│                                                                               │
│ Overall health score 53.5/100. Findings: 3 high. …                            │
╰───────────────────────────────────────────────────────────────────────────────╯
  • Self-identifies as a rival brand (anthropic) under the claimed model id gpt-4o
  • Real context window ~8000 << declared 128000 (silent truncation suspected)
  • Reported prompt tokens far exceed independent estimate
```

Reports are written to `reports/` as JSON, Markdown, and HTML.

## Knowledge base

Profiles live in [`zing/knowledge/data/`](zing/knowledge/data) as editable YAML —
one per provider (OpenAI, Anthropic, Google Gemini, DeepSeek, Qwen, GLM,
Moonshot). Each model carries its native context window, max output, tokenizer,
capability flags, identity keywords, and behavioral fingerprints. Add or override
profiles without forking:

```bash
zing check --kb-dir ./my-profiles ...     # or set ZING_KB_DIR
```

## Responsible use

zing is a black-box auditing aid. It **cannot prove**:

- that a provider stores or trains on your prompts,
- that it always routes to one exact model (relays can route probabilistically),
- billing fraud beyond what independent token estimation can suggest.

Use reports for your own due diligence. **Do not publicly accuse a vendor** based
on a single run without reviewing sample size, cost settings, and local law. Run
`zing compare` against a trusted baseline before drawing strong conclusions.

## License

[Apache-2.0](LICENSE)
