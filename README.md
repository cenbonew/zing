# zing — LLM relay reality check

> **English** · [中文](README.zh-CN.md)

[![CI](https://github.com/cenbonew/zing/actions/workflows/ci.yml/badge.svg)](https://github.com/cenbonew/zing/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**zing** is a local-first CLI that audits whether an API relay (中转站 / reseller /
proxy) actually serves the model it claims to — or quietly substitutes a cheaper
one, truncates your context window, fakes streaming, or inflates token billing
(**货不对板检测**). It speaks both **OpenAI Chat Completions** and the
**Anthropic Messages API**, and the **OpenAI Responses API** (`/v1/responses`) —
auto-detected, or forced with `--api openai|anthropic|responses`.

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

# 3) audit an Anthropic-native (Messages API) relay — protocol is auto-detected
#    from the base_url/model, or force it with --api anthropic
zing check --base-url https://relay.example.com/v1 --model claude-opus-4-8 \
  --api-key env:ZING_API_KEY --api anthropic

# 4) confirm a suspected substitution: audit the relay's REAL model id against the
#    profile it's sold as (here: a Doubao model passed off as deepseek-v4-flash)
zing check --base-url https://relay.example.com/v1 --api-key env:ZING_API_KEY \
  --model doubao-seed-2-0-lite --claimed-model deepseek-v4-flash

# 5) inspect the bundled knowledge base
zing kb            # all 85 models
zing kb deepseek   # one provider

# 6) generate a config you can commit
zing init          # writes zing.yaml
zing check -c zing.yaml
```

### As a tool for an LLM / agent

zing is built to be driven by another program or model. Everything goes to stdout
as JSON, errors included, and the exit code is the gate.

```bash
# lean, agent-friendly verdict (~5x smaller than --json: no bulky evidence)
zing check --base-url ... --model gpt-4o --compact | jq .verdict.risk

# full structured report when you need every finding's evidence
zing check --base-url ... --model gpt-4o --json

# budget first: which detectors run + estimated API calls, WITHOUT making any
zing check --base-url ... --model gpt-4o --suite deep --dry-run --json

# gate on the exit code (1 if risk >= medium); config/usage errors exit 2 as JSON
zing check --base-url ... --model gpt-4o --compact --fail-on-risk medium

# machine-readable discovery
zing kb --json                 # the whole knowledge base
zing models --base-url ... --json   # what an endpoint advertises
```

In `--json`/`--compact` mode a bad config prints `{"error": {...}}` (exit 2)
instead of a human message, so a pipeline can parse failures uniformly.

## Web UI (`zing serve`)

Prefer point-and-click? A local web UI wraps the same engine — no CLI needed.

```bash
pip install 'zing-audit[web]'
zing serve            # opens http://localhost:8000
```

Enter a relay and the model it claims; watch the audit stream **live** (per-detector
progress over SSE), then read a shareable verdict report (grade, per-dimension
breakdown, plain-language findings, downloadable JSON). It runs entirely on your
machine — a key typed in the browser reaches only your local server and the target
relay, never a third party. Bind stays on `127.0.0.1` by default.

## What it checks

zing scores nine dimensions. The three that most directly reveal 货不对板
(model identity, real context window, capability claims) carry the most weight.

| Dimension | What it catches |
|---|---|
| **model_identity** | Silent model downgrade/substitution — self-identification, knowledge-cutoff, tokenizer fingerprints, the echoed `model` field |
| **context_window** | Silent context truncation (claim 1M, recall fails at 32K) and lost-in-the-middle from cheap RAG/summarization shims, via needle-in-a-haystack + binary search |
| **capability** | Tool-calling / JSON-mode / json-schema / max-output claims that aren't actually delivered (or *over*-delivered, hinting at a substitute); and **vision** — a model claiming image input is sent a known-answer generated image to confirm it actually "sees" |
| **billing** | Token/usage inflation and missing/unverifiable usage accounting, via an independent tokenizer estimate |
| **streaming** | Fake streaming (buffer-then-chunk) detected from chunk count and inter-chunk timing |
| **protocol** | OpenAI-compatibility conformance: multi-turn, stop sequences, response shape, error schema — and a determinism sub-check for response caching that ignores temperature/seed |
| **reliability** | Concurrent success rate and latency (HTTP 429 throttling bucketed separately) |
| **connectivity** | Endpoint reachability and the advertised `/v1/models` list |
| **security** | Transport (HTTPS), header hygiene, secret echo; hidden injected system prompt (fixed input-token overhead + leak), in-flight response/tool-call tampering via known-answer canaries (URL/package substitution), and prompt-prefix caching (timing) |

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

## Monitoring (`zing watch`)

Relays can serve the real model today and quietly swap it next week. `zing watch`
re-audits on a schedule, records each run to history, and alerts a webhook when the
risk crosses a threshold or **regresses** versus the previous run.

```bash
zing watch --base-url https://relay.example.com/v1 --api-key env:ZING_API_KEY \
  --model gpt-4o --suite standard --interval 3600 \
  --alert-on medium --webhook "$FEISHU_WEBHOOK"      # or --once for cron
```

Alerts are formatted for **Slack / Feishu (飞书) / DingTalk (钉钉) / generic JSON**,
auto-detected from the webhook URL.

Prefer a UI? `zing serve` has a built-in monitor at **`/watches`** (🔔 监控): add a
watch in the browser and an in-process background scheduler re-runs it on its interval,
persists every run to history, and fires the same webhook alerts on a threshold cross or
regression. Run-now / pause / delete from the page. Keys are stored only in `~/.zing` and
never returned to the browser.

## Embedding & rerank audits

Embeddings and rerank are a non-chat surface, so zing audits them with a focused
standalone auditor instead of the 9-dimension chat pipeline.

```bash
# Expected vector dimension is resolved from the bundled KB for the claimed model.
zing embed --base-url https://relay.example.com/v1 \
           --model text-embedding-3-large --claimed-model text-embedding-3-large --fail-on-risk high

# Or override the expected dimension directly:
zing embed --base-url ... --model my-embed --claimed-dimensions 1024 --json

# Rerank: a built-in known-answer probe — a genuine reranker must rank the
# obviously-relevant document first.
zing rerank --base-url https://relay.example.com/v1 --model my-rerank
```

`embed` checks connectivity, **dimension match** (returned vector length vs the claimed
model's native dimension — the headline 货不对板 signal; a relay claiming 3072-d
`text-embedding-3-large` but returning 1024-d is a substituted model), determinism (same
input → cosine ≈ 1), distinctness (unrelated inputs → cosine well below 1), and the
echoed `model` field. Bundled KB profiles: OpenAI `text-embedding-3-small` (1536),
`text-embedding-3-large` (3072), `text-embedding-ada-002` (1536), Qwen
`text-embedding-v3`/`-v4` (1024).

Both also live in the web UI — `zing serve` has a **工具箱 / Tools** page at `/tools`
(linked from the nav) with embed/rerank forms that render the same localized verdict.

## Image & audio (TTS) generation audits

Two more non-chat surfaces: image generation (`POST /v1/images/generations`) and
text-to-speech (`POST /v1/audio/speech`). All decoding is pure stdlib — image
dimensions from header bytes (PNG/JPEG/GIF/WebP), WAV duration via the `wave` module.

```bash
# Does a relay claiming DALL·E 3 actually return the requested 1792x1024? A
# downscaled / wrong-size image (or a size outside the claimed model's native sizes,
# resolved from the KB) is the headline 货不对板 signal.
zing image --base-url https://relay.example.com/v1 --api-key env:RELAY_KEY \
  --model dall-e-3 --claimed-model dall-e-3 --size 1792x1024 --fail-on-risk high

# Does a relay claiming tts-1-hd return real audio whose length scales with the input
# (not a fixed placeholder, not HTML/JSON masquerading as audio)?
zing audio --base-url https://relay.example.com/v1 --api-key env:RELAY_KEY \
  --model tts-1-hd --voice alloy --format wav --save clip.wav
```

`image` checks: connectivity, valid/decodable format, **size match** (decoded WxH vs the
request and the claimed model's native sizes — FAIL/HIGH on mismatch), distinctness (two
prompts → different images, catching a fixed placeholder), count, model field. `audio`
checks: connectivity, container/format validity, format honored, non-trivial duration
(scales with input length), distinctness, model field. KB ships OpenAI DALL·E 2/3,
gpt-image-1, tts-1/tts-1-hd/gpt-4o-mini-tts, and Qwen image/TTS profiles.

## Use in CI (GitHub Action)

Gate any workflow on a relay audit with the bundled composite action. It runs
`zing check --compact --fail-on-risk`, exposes `risk` / `score` / `rating` as outputs,
writes a summary to the run, and fails the job when the risk gate trips.

```yaml
jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - id: zing
        uses: cenbonew/zing@v0.9.0          # pin to a release tag
        with:
          base-url: https://relay.example.com/v1
          api-key: ${{ secrets.RELAY_API_KEY }}   # caller secret; never echoed
          model: gpt-4o
          fail-on-risk: high
      - run: echo "risk=${{ steps.zing.outputs.risk }} score=${{ steps.zing.outputs.score }}"
```

The relay key is forwarded via an environment variable (`--api-key env:…`), so it never
appears on a command line. See [docs/CI.md](docs/CI.md) for the full inputs/outputs table
and a deploy-gating example.

## Suites

| Suite | Detectors | Cost |
|---|---|---|
| `smoke` | connectivity, security | very low |
| `standard` | + protocol, model_identity, capability, streaming, billing, reliability | low–medium |
| `deep` | + context_window, determinism, injected_prompt, integrity, prompt_cache, quality_judge (if `--judge`) | higher (long-context & timing probes cost tokens) |
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
