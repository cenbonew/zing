"""Config loading, secret resolution, and run options.

A run can be configured entirely on the command line or via a YAML file (see
``TEMPLATE``). CLI flags always win over file values.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from zing.models import TargetConfig

SUITES = ("smoke", "standard", "deep", "full")
FORMATS = ("json", "md", "html", "all")
RISK_LEVELS = ("low", "medium", "high")


class ConfigError(Exception):
    """Raised for user-facing configuration problems."""


class AuditOptions(BaseModel):
    """Knobs that control which detectors run and how aggressively."""

    model_config = ConfigDict(extra="forbid")

    suite: str = "standard"
    judge: bool = False
    only: list[str] = Field(default_factory=list)   # run only these detector ids
    skip: list[str] = Field(default_factory=list)    # skip these detector ids

    # Context-window probe: cap so a claimed 1M model does not cost a fortune.
    max_context_probe_tokens: int = 200_000
    context_probe_floor_tokens: int = 1_000

    # Reliability probe.
    reliability_requests: int = 8
    reliability_concurrency: int = 3

    # Identity/determinism sampling.
    determinism_samples: int = 3

    def enabled(self, detector_id: str) -> bool:
        if self.only:
            return detector_id in self.only
        return detector_id not in self.skip


def resolve_secret(value: str | None) -> str:
    """Resolve a secret reference.

    Supports ``env:VAR`` (read from environment), ``file:/path`` (read file
    contents, stripped), or a raw literal. Empty/None resolves to "".
    """
    if not value:
        return ""
    if value.startswith("env:"):
        var = value[len("env:"):]
        return os.environ.get(var, "")
    if value.startswith("file:"):
        path = Path(value[len("file:"):]).expanduser()
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"Could not read secret file {path}: {exc}") from exc
    return value


def load_config_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a mapping at the top level")
    return data


def section(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    value = cfg.get(name)
    return value if isinstance(value, dict) else {}


def merge_headers(
    file_headers: dict[str, str] | None, cli_headers: list[str] | None
) -> dict[str, str]:
    merged: dict[str, str] = dict(file_headers or {})
    for raw in cli_headers or []:
        if ":" not in raw:
            raise ConfigError(f"Header must be in 'Name: value' form: {raw!r}")
        key, _, val = raw.partition(":")
        merged[key.strip()] = val.strip()
    return merged


def build_target(
    *,
    kind: str,
    name: str | None,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    declared_provider: str | None = None,
    timeout_sec: float | None = None,
    headers: dict[str, str] | None = None,
) -> TargetConfig:
    if not base_url:
        raise ConfigError(f"{kind}: base_url is required")
    base_url = base_url.strip()
    if not base_url.lower().startswith(("http://", "https://")):
        raise ConfigError(
            f"{kind}: base_url must start with http:// or https:// (got {base_url!r})"
        )
    if not model:
        raise ConfigError(f"{kind}: model is required")
    return TargetConfig(
        name=name or kind,
        kind=kind,
        base_url=base_url,
        api_key=resolve_secret(api_key),
        model=model,
        declared_provider=declared_provider,
        timeout_sec=timeout_sec if timeout_sec is not None else 60.0,
        headers=headers or {},
    )


def validate_suite(value: str) -> str:
    if value not in SUITES:
        raise ConfigError(f"Unknown suite {value!r}. Choose from: {', '.join(SUITES)}")
    return value


def validate_format(value: str) -> str:
    if value not in FORMATS:
        raise ConfigError(f"Unknown format {value!r}. Choose from: {', '.join(FORMATS)}")
    return value


def validate_risk(value: str | None) -> str | None:
    """Validate a --fail-on-risk threshold up front.

    A typo here (e.g. ``--fail-on-risk hihg``) must fail loudly rather than silently
    disabling the CI gate, so an unknown value raises instead of being ignored.
    """
    if value is None:
        return None
    if value not in RISK_LEVELS:
        raise ConfigError(
            f"Unknown risk level {value!r}. Choose from: {', '.join(RISK_LEVELS)}"
        )
    return value


TEMPLATE = """\
# zing configuration — LLM relay reality check
# Run:  zing check -c zing.yaml
# Docs: https://github.com/cenbonew/zing

target:
  name: my-relay
  base_url: https://relay.example.com/v1
  api_key: env:ZING_API_KEY        # env:VAR | file:/path | raw value
  model: gpt-4o
  declared_provider: openai        # optional; inferred from model id if omitted
  timeout_sec: 60
  headers: {}

# Optional trusted baseline for `zing compare` (strongest downgrade evidence).
baseline:
  name: openai-official
  base_url: https://api.openai.com/v1
  api_key: env:OPENAI_API_KEY
  model: gpt-4o

run:
  suite: standard                  # smoke | standard | deep | full
  judge: false                     # enable code+LLM hybrid judging
  output_dir: reports
  format: all                      # json | md | html | all
  reliability_requests: 8
  concurrency: 3
  max_context_probe_tokens: 200000 # cap for the real-context-window probe

# Optional LLM judge backend (used when run.judge is true).
# Defaults to the baseline endpoint if omitted.
judge:
  base_url: https://api.openai.com/v1
  api_key: env:OPENAI_API_KEY
  model: gpt-4o-mini
"""
