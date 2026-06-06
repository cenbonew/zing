"""Turn detector results into dimension scores and a headline verdict.

The verdict deliberately uses cautious, evidence-first language: zing reports
*divergence and risk*, never a definitive fraud accusation. The dimensions that
speak to "货不对板" (model identity, real context window, capability claims) carry
the most weight.
"""

from __future__ import annotations

from zing.models import (
    DetectorResult,
    Dimension,
    DimensionScore,
    Finding,
    ReliabilitySummary,
    RiskLevel,
    Severity,
    Status,
    Verdict,
)

# Weights sum to 100. The substitution/truncation-revealing dimensions dominate.
DIMENSION_WEIGHTS: dict[Dimension, float] = {
    Dimension.CONNECTIVITY: 8.0,
    Dimension.PROTOCOL: 8.0,
    Dimension.CONTEXT_WINDOW: 20.0,
    Dimension.MODEL_IDENTITY: 22.0,
    Dimension.CAPABILITY: 14.0,
    Dimension.STREAMING: 6.0,
    Dimension.BILLING: 8.0,
    Dimension.RELIABILITY: 8.0,
    Dimension.SECURITY: 6.0,
}

# Dimensions whose failure most directly indicates 货不对板.
_CORE_DIMENSIONS = (
    Dimension.MODEL_IDENTITY,
    Dimension.CONTEXT_WINDOW,
    Dimension.CAPABILITY,
)

# Pure transport/reachability dimensions. A HIGH here (relay down, throttled,
# transient 5xx) means we could not assess the model — NOT that the relay serves a
# different model — so it must not escalate the substitution risk ladder. Its score
# still counts; it just can't drive the "diverges from the claimed model" headline.
_INFRA_DIMENSIONS = (Dimension.CONNECTIVITY,)

# Statuses that count as a core dimension having produced a usable substitution signal.
_USABLE_STATUSES = (Status.PASS, Status.WARN, Status.FAIL)

_SEVERITY_ORDER = [
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
]


def _status_from_score(score: float | None) -> Status:
    if score is None:
        return Status.NOT_RUN
    if score >= 85:
        return Status.PASS
    if score >= 70:
        return Status.WARN
    return Status.FAIL


# How "bad" a status is, for picking the worst across a dimension's detectors.
# Substantive conclusions (WARN/FAIL) outrank the operational ERROR so a crashed
# sibling detector cannot mask a real WARN/FAIL in the same dimension; ERROR still
# outranks PASS/INCONCLUSIVE so a lone crash surfaces as a coverage gap.
_STATUS_BADNESS = {
    Status.NOT_RUN: 0,
    Status.INFO: 1,
    Status.PASS: 2,
    Status.INCONCLUSIVE: 3,
    Status.ERROR: 4,
    Status.WARN: 5,
    Status.FAIL: 6,
}


def _all_findings(detectors: list[DetectorResult]) -> list[tuple[DetectorResult, Finding]]:
    return [(d, f) for d in detectors for f in d.findings]


def _worst_status(statuses: list[Status]) -> Status:
    if not statuses:
        return Status.NOT_RUN
    return max(statuses, key=lambda s: _STATUS_BADNESS.get(s, 0))


def build_dimensions(
    detectors: list[DetectorResult], reliability: ReliabilitySummary | None
) -> list[DimensionScore]:
    dimensions: list[DimensionScore] = []
    for dim, weight in DIMENSION_WEIGHTS.items():
        members = [d for d in detectors if d.dimension == dim]
        scored = [d.score for d in members if d.score is not None]
        score = round(sum(scored) / len(scored), 1) if scored else None

        # Status reflects what the detectors themselves concluded — we do NOT
        # re-derive FAIL purely from a soft score, which would over-escalate.
        status = _worst_status([d.status for d in members])
        # But a serious finding always pulls the dimension down.
        worst = Severity.INFO
        for det in members:
            ws = det.worst_severity()
            if _SEVERITY_ORDER.index(ws) > _SEVERITY_ORDER.index(worst):
                worst = ws
        if worst in (Severity.HIGH, Severity.CRITICAL) and members:
            status = Status.FAIL
        elif worst == Severity.MEDIUM and status in (Status.PASS, Status.INFO):
            status = Status.WARN

        if not members:
            reason = "Not run in this suite."
        elif score is None:
            reason = "Ran but produced no numeric score (see findings)."
        else:
            reason = "; ".join(
                f.title for det in members for f in det.findings
                if f.status in (Status.FAIL, Status.WARN)
            )[:200] or "All checks passed."
        dimensions.append(
            DimensionScore(dimension=dim, score=score, weight=weight, status=status, reason=reason)
        )
    return dimensions


def overall_score(dimensions: list[DimensionScore]) -> float | None:
    scored = [d for d in dimensions if d.score is not None]
    if not scored:
        return None
    weighted = sum((d.score or 0.0) * d.weight for d in scored)
    total = sum(d.weight for d in scored)
    return round(weighted / total, 1) if total else None


def _rating(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"


def build_verdict(
    detectors: list[DetectorResult],
    dimensions: list[DimensionScore],
    *,
    profile_matched: bool,
    used_judge: bool,
    used_baseline: bool,
) -> Verdict:
    score = overall_score(dimensions)
    pairs = _all_findings(detectors)

    # Substitution risk is judged on what is SERVED, excluding pure transport
    # findings (a down/throttled honest relay must not read as "diverges").
    infra_dims = set(_INFRA_DIMENSIONS)
    substantive = [(d, f) for d, f in pairs if d.dimension not in infra_dims]
    critical = [f for _, f in substantive if f.severity == Severity.CRITICAL]
    high = [f for _, f in substantive if f.severity == Severity.HIGH]
    medium = [f for _, f in substantive if f.severity == Severity.MEDIUM]

    # Findings landing in a core (substitution-revealing) dimension weigh more.
    core_dims = set(_CORE_DIMENSIONS)
    core_high = [f for d, f in pairs if d.dimension in core_dims and f.severity in (Severity.HIGH, Severity.CRITICAL)]
    core_medium = [f for d, f in pairs if d.dimension in core_dims and f.severity == Severity.MEDIUM]
    # Core coverage measured over distinct DIMENSIONS that produced a usable signal,
    # not over detector count — two MODEL_IDENTITY detectors must not look like full
    # coverage while CONTEXT_WINDOW/CAPABILITY never ran.
    core_dims_ran = {
        d.dimension
        for d in detectors
        if d.dimension in core_dims and d.status in _USABLE_STATUSES
    }
    judge_effective = any(
        d.id == "quality_judge" and d.status in _USABLE_STATUSES for d in detectors
    )

    # --- risk classification: driven by FINDING SEVERITY, not score alone --- #
    # zing only reaches HIGH on hard, high-severity evidence — a single soft
    # signal or a merely-low score never escalates to HIGH on its own.
    if not core_dims_ran:
        # No core dimension produced a usable substitution signal (relay unreachable,
        # unprofiled, or only peripheral checks ran) — we cannot judge the claim.
        # Peripheral findings are still carried in the report as advisories.
        risk = RiskLevel.INCONCLUSIVE
    elif critical or core_high or len(high) >= 2:
        risk = RiskLevel.HIGH
    elif high or core_medium:  # exactly one high-severity finding outside the core dimensions
        risk = RiskLevel.MEDIUM
    elif medium:
        risk = RiskLevel.LOW
    else:
        risk = RiskLevel.CLEAN

    # --- confidence -------------------------------------------------------- #
    n_core_total = len(core_dims)
    if not profile_matched:
        confidence = "low"
    elif used_baseline and len(core_dims_ran) == n_core_total:
        confidence = "high"
    elif len(core_dims_ran) >= 2:
        confidence = "medium"
    else:
        confidence = "low"
    # Only let an *effective* judge (one that produced a usable verdict, not merely a
    # constructed client) lift confidence.
    if judge_effective and confidence == "medium":
        confidence = "high"

    headline = _headline(risk, confidence)
    summary = _summary(risk, score, critical, high, medium, profile_matched, used_baseline)
    key = [f.title for f in (critical + high + medium)][:6]

    return Verdict(
        overall_score=score,
        rating=_rating(score),
        risk_level=risk,
        headline=headline,
        confidence=confidence,
        summary=summary,
        key_findings=key,
    )


def _headline(risk: RiskLevel, confidence: str) -> str:
    table = {
        RiskLevel.CLEAN: "Behavior is consistent with the claimed model.",
        RiskLevel.LOW: "Mostly consistent with the claimed model; minor concerns.",
        RiskLevel.MEDIUM: "Some behavior diverges from the claimed model — investigate.",
        RiskLevel.HIGH: "Strong evidence the relay does not deliver the claimed model as advertised.",
        RiskLevel.INCONCLUSIVE: "Not enough signal to judge — connectivity or coverage was insufficient.",
    }
    return f"{table[risk]} (confidence: {confidence})"


def _summary(
    risk: RiskLevel,
    score: float | None,
    critical: list[Finding],
    high: list[Finding],
    medium: list[Finding],
    profile_matched: bool,
    used_baseline: bool,
) -> str:
    bits: list[str] = []
    if score is not None:
        bits.append(f"Overall health score {score}/100.")
    counts = []
    if critical:
        counts.append(f"{len(critical)} critical")
    if high:
        counts.append(f"{len(high)} high")
    if medium:
        counts.append(f"{len(medium)} medium")
    if counts:
        bits.append("Findings: " + ", ".join(counts) + ".")
    else:
        bits.append("No significant divergence findings.")
    if not profile_matched:
        bits.append(
            "The claimed model was not found in the knowledge base, so identity/"
            "capability checks are limited — pass --declared-provider or add a KB profile."
        )
    if not used_baseline and risk in (RiskLevel.MEDIUM, RiskLevel.HIGH):
        bits.append("Run `zing compare` against a trusted baseline to strengthen the verdict.")
    bits.append(
        "zing reports black-box evidence of divergence and risk, not proof of fraud."
    )
    return " ".join(bits)
