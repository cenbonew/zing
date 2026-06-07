# Changelog

All notable changes to **zing** are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — accuracy pass on real relays (DeepSeek / Doubao)

Validated against live endpoints (DeepSeek official + Aliyun, Volcengine, …): honest
DeepSeek relays now read CLEAN, and Doubao models passed off as DeepSeek are caught HIGH.

### Added

- **`--claimed-model`** — audit an endpoint's *real* model id against a *different*
  claimed model's profile (e.g. request `doubao-seed-...` but verify it against the
  `deepseek-v4-flash` profile). Lets you confirm a suspected substitution end-to-end.

### Fixed

- **Billing false positives on reasoning models / non-OpenAI tokenizers.** A reasoning
  model's `completion_tokens` legitimately includes hidden reasoning tokens the
  visible-text estimate can't see, and heuristic (non-tiktoken) estimates are
  imprecise — these no longer produce a "token inflation" HIGH. Prompt-token thresholds
  widen for heuristic tokenizers, and prompt-padding is still checked when a reasoning
  model returns empty visible content. (DeepSeek official went MEDIUM → CLEAN.)
- **Substitution false negatives.** Model-identity now flags ANY known vendor brand
  that isn't the model's own — including Doubao/ByteDance, Kimi, GLM, Hunyuan, Ernie,
  MiniMax (with Chinese names) — so a substitute the per-model KB list never enumerated
  is still caught. (A "I'm Doubao, by ByteDance" relay sold as DeepSeek now reads HIGH.)
- KB: added DeepSeek's native brand name (深度求索) to the deepseek profiles so a
  genuine model using it isn't mis-flagged.

## [0.2.1]

### Fixed

- `zing --version` now reports the actual installed version. It was hardcoded in
  `zing/__init__.py` and reported `0.1.0` for the 0.2.0 release; the version is now
  single-sourced from package metadata (`importlib.metadata`) so it can never drift
  from `pyproject.toml`.

## [0.2.0] — Anthropic support & roadmap security detectors

### Added

- **Anthropic Messages API support.** zing now audits Anthropic-native relays
  (`/v1/messages`) as well as OpenAI Chat Completions, behind one detector
  interface. The protocol is auto-detected from the base_url/model or forced with
  `--api openai|anthropic` (and `--target-api` / `--baseline-api` for `compare`).
- **Three new `deep`-suite security detectors** (formerly roadmap):
  - `injected_prompt` — detects a hidden, silently-prepended system prompt from a
    large *fixed* input-token overhead (measured across two message sizes) plus a
    leak probe; needs both signals to warn.
  - `integrity` — known-answer URL/package canaries catch in-flight response/tool-call
    tampering (value substituted, structure preserved). CRITICAL only when a trusted
    baseline returns the canary intact; otherwise MEDIUM.
  - `prompt_cache` — flags prompt-prefix caching by TTFT timing (informational; states
    that cross-user cache sharing is not provable from a single key).
- Automated PyPI publishing via GitHub Actions + Trusted Publishing
  (`.github/workflows/release.yml`); see `docs/PUBLISHING.md`.

## [0.1.0] — first public alpha

First public release. A local-first CLI that audits whether an OpenAI-compatible
API relay (中转站 / reseller / proxy) actually serves the model it claims to —
or quietly substitutes a cheaper one, truncates the context window, fakes
streaming, or inflates token billing (货不对板检测).

### Added

- `zing check` — audit one relay endpoint against what it claims (model id +
  optional provider hint).
- `zing compare` — audit a relay against a trusted baseline of the same declared
  model (the strongest downgrade evidence).
- `zing models` — probe an endpoint's `GET /v1/models` list.
- `zing kb` — inspect the bundled knowledge base.
- `zing init` — write a starter `zing.yaml` config.
- Eleven detectors across nine scored dimensions: model identity & downgrade
  fingerprinting, real context window & truncation, capability claims, token/usage
  billing, streaming authenticity, OpenAI-protocol conformance, determinism/cache
  correctness, concurrent reliability, transport/secret-handling security, plus
  connectivity and an optional LLM-judged quality assessment (`--judge`).
- Bundled knowledge base of **85 native model profiles across 7 platforms**
  (OpenAI, Anthropic, Google Gemini, DeepSeek, Qwen, GLM, Moonshot), editable as
  YAML and overridable via `--kb-dir` / `ZING_KB_DIR`.
- Two detection modes: pure-code deterministic probes (default) and a code+LLM
  hybrid that consults a separate trusted judge model.
- JSON, Markdown, and HTML reports; `--json` for machine/agent consumption.
- Evidence-first verdicts (CLEAN / LOW / MEDIUM / HIGH / INCONCLUSIVE) with
  confidence, designed to avoid false accusations of honest relays.
- Secret hygiene: API keys are fingerprinted (never stored) and relay-controlled
  text is redacted before it reaches any report (JSON/Markdown/HTML).
- `--fail-under` / `--fail-on-risk` exit-code gates for CI use.

[Unreleased]: https://github.com/cenbonew/zing/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/cenbonew/zing/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/cenbonew/zing/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/cenbonew/zing/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cenbonew/zing/releases/tag/v0.1.0
