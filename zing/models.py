"""Core data contracts shared across zing.

Everything that crosses a module boundary — config, a single API call's outcome,
a detector's findings, the scored report — is defined here as a pydantic model so
the JSON report is well-typed and detectors compose cleanly. Detector authors
should treat these types as the stable interface.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    """Outcome of a check, finding, or dimension."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"
    INFO = "info"
    ERROR = "error"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Dimension(str, Enum):
    """Scoring dimensions. Each detector contributes to exactly one."""

    CONNECTIVITY = "connectivity"
    PROTOCOL = "protocol"
    CONTEXT_WINDOW = "context_window"
    MODEL_IDENTITY = "model_identity"
    CAPABILITY = "capability"
    STREAMING = "streaming"
    BILLING = "billing"
    RELIABILITY = "reliability"
    SECURITY = "security"


class RiskLevel(str, Enum):
    """Headline 货不对板 risk classification."""

    CLEAN = "clean"          # consistent with the claimed model
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"            # strong evidence of mismatch / downgrade
    INCONCLUSIVE = "inconclusive"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
class TargetConfig(BaseModel):
    """A relay endpoint under audit (or a trusted baseline)."""

    model_config = ConfigDict(extra="forbid")

    name: str = "target"
    kind: str = "target"  # "target" | "baseline"
    base_url: str
    api_key: str = ""
    model: str  # the model id actually sent in requests
    # The model the relay CLAIMS to serve (for KB lookup / comparison). Defaults to
    # `model`. Set it to audit an endpoint's real model id against a different claim
    # — e.g. request `doubao-...` but verify it against the `deepseek-v4-flash` profile.
    claimed_model: str | None = None
    # Wire protocol: "auto" infers from the base_url/model, or force "openai"
    # (Chat Completions) / "anthropic" (Messages API).
    api: str = "auto"

    @property
    def claimed(self) -> str:
        return self.claimed_model or self.model
    # Optional declared metadata used to pick the right knowledge-base profile and
    # to compare claims vs reality. If absent, zing infers from the model id.
    declared_provider: str | None = None
    declared_context_window: int | None = None
    declared_max_output: int | None = None
    timeout_sec: float = 60.0
    headers: dict[str, str] = Field(default_factory=dict)
    max_retries: int = 0


class RequestSpec(BaseModel):
    """A single chat-completion request the client should issue."""

    model_config = ConfigDict(extra="forbid")

    messages: list[dict[str, Any]]
    temperature: float | None = 0.0
    max_tokens: int | None = None
    top_p: float | None = None
    stop: str | list[str] | None = None
    seed: int | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    stream: bool = False
    extra_body: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Raw call outcome
# --------------------------------------------------------------------------- #
class CompletionOutcome(BaseModel):
    """Raw evidence from one API call. Detectors interpret these."""

    model_config = ConfigDict(extra="forbid")

    ok: bool = False
    status_code: int | None = None
    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    model_returned: str | None = None

    # Timing
    duration_ms: float | None = None
    ttft_ms: float | None = None  # time to first streamed token
    # Per-event arrival offsets (ms from request start), for fake-stream analysis.
    chunk_timings_ms: list[float] = Field(default_factory=list)
    chunk_count: int = 0

    # Transport / error evidence (redacted before it ever reaches here).
    headers: dict[str, str] = Field(default_factory=dict)
    error_type: str | None = None
    error_message: str | None = None
    raw_error: dict[str, Any] | None = None

    def has_content(self) -> bool:
        return bool(self.content and self.content.strip())


# --------------------------------------------------------------------------- #
# Detector output
# --------------------------------------------------------------------------- #
class Finding(BaseModel):
    """A single evidence-bearing observation produced by a detector."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: Status
    severity: Severity = Severity.INFO
    summary: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    recommendation: str | None = None


class DetectorResult(BaseModel):
    """The result of running one detector."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    dimension: Dimension
    status: Status = Status.NOT_RUN
    # 0-100 quality/health score for this detector, or None if not scorable.
    score: float | None = None
    findings: list[Finding] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    duration_ms: float | None = None
    error: str | None = None
    # True when this detector required an LLM judge to produce its verdict.
    used_judge: bool = False

    def worst_severity(self) -> Severity:
        order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        worst = Severity.INFO
        for finding in self.findings:
            if order.index(finding.severity) > order.index(worst):
                worst = finding.severity
        return worst


# --------------------------------------------------------------------------- #
# Scoring & verdict
# --------------------------------------------------------------------------- #
class DimensionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: Dimension
    score: float | None = None
    weight: float = 0.0
    status: Status = Status.NOT_RUN
    reason: str = ""


class ReliabilitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests: int = 0
    successes: int = 0
    success_rate: float = 0.0
    # HTTP 429s — the relay correctly throttling a concurrency burst, an honest
    # behavior. Tracked apart from `errors` so it doesn't tank the success rate.
    rate_limited: int = 0
    latency_ms: dict[str, float | None] = Field(default_factory=dict)
    errors: dict[str, int] = Field(default_factory=dict)


class Verdict(BaseModel):
    """The headline judgement a user reads first."""

    model_config = ConfigDict(extra="forbid")

    overall_score: float | None = None
    rating: str | None = None  # A-F
    risk_level: RiskLevel = RiskLevel.INCONCLUSIVE
    headline: str = ""
    confidence: str = "low"  # low | medium | high
    summary: str = ""
    key_findings: list[str] = Field(default_factory=list)


class RedactedTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: str
    base_url: str
    model: str
    claimed_model: str | None = None
    declared_provider: str | None = None
    api_key_fingerprint: str | None = None


# --------------------------------------------------------------------------- #
# Top-level report
# --------------------------------------------------------------------------- #
class AuditReport(BaseModel):
    """The serializable artifact zing produces. JSON form is the LLM-facing API."""

    model_config = ConfigDict(extra="forbid")

    tool_version: str
    mode: str  # "check" | "compare"
    generated_at: str | None = None  # ISO timestamp, stamped by the runner
    command: str | None = None
    suite: str

    target: RedactedTarget
    baseline: RedactedTarget | None = None

    verdict: Verdict
    dimensions: list[DimensionScore] = Field(default_factory=list)
    detectors: list[DetectorResult] = Field(default_factory=list)
    baseline_detectors: list[DetectorResult] = Field(default_factory=list)
    reliability: ReliabilitySummary | None = None

    judge_used: bool = False
    judge_model: str | None = None

    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
