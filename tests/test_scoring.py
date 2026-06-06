"""Tests for dimension scoring and the headline verdict.

These exercise the spine that turns raw detector findings into a risk level,
using synthetic :class:`DetectorResult` lists so the logic is isolated from any
real relay behaviour.
"""

from __future__ import annotations

from zing.models import (
    DetectorResult,
    Dimension,
    Finding,
    RiskLevel,
    Severity,
    Status,
)
from zing.scoring import build_dimensions, build_verdict, overall_score


def _result(
    dimension: Dimension,
    *,
    score: float | None,
    status: Status,
    findings: list[Finding] | None = None,
) -> DetectorResult:
    return DetectorResult(
        id=f"{dimension.value}-det",
        name=dimension.value,
        dimension=dimension,
        score=score,
        status=status,
        findings=findings or [],
    )


def _info(dim_id: str) -> Finding:
    return Finding(id=f"{dim_id}.ok", title="All good", status=Status.PASS, severity=Severity.INFO)


def _clean_core_detectors() -> list[DetectorResult]:
    """One passing detector for each core (substitution-revealing) dimension."""
    return [
        _result(Dimension.MODEL_IDENTITY, score=95.0, status=Status.PASS, findings=[_info("identity")]),
        _result(Dimension.CONTEXT_WINDOW, score=92.0, status=Status.PASS, findings=[_info("context")]),
        _result(Dimension.CAPABILITY, score=90.0, status=Status.PASS, findings=[_info("capability")]),
    ]


# --------------------------------------------------------------------------- #
# build_dimensions
# --------------------------------------------------------------------------- #
def test_build_dimensions_covers_every_dimension():
    dims = build_dimensions([], reliability=None)
    assert {d.dimension for d in dims} == set(Dimension)
    # Nothing ran -> NOT_RUN with no score.
    for d in dims:
        assert d.status == Status.NOT_RUN
        assert d.score is None


def test_build_dimensions_averages_member_scores():
    detectors = [
        _result(Dimension.CONNECTIVITY, score=80.0, status=Status.PASS),
        _result(Dimension.CONNECTIVITY, score=100.0, status=Status.PASS),
    ]
    dims = {d.dimension: d for d in build_dimensions(detectors, reliability=None)}
    conn = dims[Dimension.CONNECTIVITY]
    assert conn.score == 90.0
    assert conn.status == Status.PASS


def test_high_severity_finding_forces_dimension_fail():
    findings = [
        Finding(
            id="identity.mismatch",
            title="Self-identification mismatch",
            status=Status.WARN,
            severity=Severity.HIGH,
        )
    ]
    # Even with a decent score, a HIGH finding pulls the dimension to FAIL.
    detectors = [_result(Dimension.MODEL_IDENTITY, score=75.0, status=Status.WARN, findings=findings)]
    dims = {d.dimension: d for d in build_dimensions(detectors, reliability=None)}
    assert dims[Dimension.MODEL_IDENTITY].status == Status.FAIL


# --------------------------------------------------------------------------- #
# overall_score
# --------------------------------------------------------------------------- #
def test_overall_score_none_when_nothing_scored():
    dims = build_dimensions([], reliability=None)
    assert overall_score(dims) is None


def test_overall_score_is_weighted():
    detectors = _clean_core_detectors()
    dims = build_dimensions(detectors, reliability=None)
    score = overall_score(dims)
    assert score is not None
    assert 85.0 <= score <= 100.0


# --------------------------------------------------------------------------- #
# build_verdict
# --------------------------------------------------------------------------- #
def test_verdict_clean_case():
    detectors = _clean_core_detectors()
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=True, used_judge=False, used_baseline=False
    )
    assert verdict.risk_level == RiskLevel.CLEAN
    assert verdict.overall_score is not None
    assert verdict.rating in ("A", "B")
    assert "consistent" in verdict.headline.lower()


def test_verdict_high_risk_on_core_high_severity_identity_finding():
    finding = Finding(
        id="identity.downgrade",
        title="Likely model downgrade",
        status=Status.FAIL,
        severity=Severity.HIGH,
    )
    detectors = [
        _result(Dimension.MODEL_IDENTITY, score=20.0, status=Status.FAIL, findings=[finding]),
        _result(Dimension.CONTEXT_WINDOW, score=90.0, status=Status.PASS, findings=[_info("context")]),
        _result(Dimension.CAPABILITY, score=90.0, status=Status.PASS, findings=[_info("capability")]),
    ]
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=True, used_judge=False, used_baseline=False
    )
    assert verdict.risk_level == RiskLevel.HIGH
    assert "Likely model downgrade" in verdict.key_findings


def test_verdict_inconclusive_when_no_core_dimensions_ran():
    # Only connectivity ran — no substitution-revealing evidence at all.
    detectors = [
        _result(Dimension.CONNECTIVITY, score=100.0, status=Status.PASS, findings=[_info("conn")]),
    ]
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=True, used_judge=False, used_baseline=False
    )
    assert verdict.risk_level == RiskLevel.INCONCLUSIVE


def test_verdict_single_noncore_high_is_medium():
    finding = Finding(
        id="streaming.fake",
        title="Streaming may be buffered",
        status=Status.WARN,
        severity=Severity.HIGH,
    )
    detectors = [
        *_clean_core_detectors(),
        _result(Dimension.STREAMING, score=40.0, status=Status.WARN, findings=[finding]),
    ]
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=True, used_judge=False, used_baseline=False
    )
    # A single high-severity finding outside the core dimensions caps at MEDIUM.
    assert verdict.risk_level == RiskLevel.MEDIUM


def test_verdict_connectivity_high_does_not_escalate_clean_cores():
    # A transport/connectivity HIGH (relay down/flaky) must NOT make an otherwise
    # clean set of core checks read as "diverges from the claimed model".
    conn_fail = Finding(
        id="connectivity.unreachable",
        title="Endpoint unreachable",
        status=Status.FAIL,
        severity=Severity.HIGH,
    )
    detectors = [
        *_clean_core_detectors(),
        _result(Dimension.CONNECTIVITY, score=0.0, status=Status.FAIL, findings=[conn_fail]),
    ]
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=True, used_judge=False, used_baseline=False
    )
    assert verdict.risk_level == RiskLevel.CLEAN


def test_verdict_unreachable_relay_is_inconclusive_not_medium():
    # Connectivity HIGH while the core detectors all errored out -> no usable
    # substitution signal -> INCONCLUSIVE, never a divergence verdict.
    conn_fail = Finding(
        id="connectivity.unreachable",
        title="Endpoint unreachable",
        status=Status.FAIL,
        severity=Severity.HIGH,
    )
    detectors = [
        _result(Dimension.CONNECTIVITY, score=0.0, status=Status.FAIL, findings=[conn_fail]),
        _result(Dimension.MODEL_IDENTITY, score=None, status=Status.ERROR),
        _result(Dimension.CAPABILITY, score=None, status=Status.ERROR),
    ]
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=True, used_judge=False, used_baseline=False
    )
    assert verdict.risk_level == RiskLevel.INCONCLUSIVE


def test_verdict_confidence_low_when_profile_unmatched():
    detectors = _clean_core_detectors()
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=False, used_judge=False, used_baseline=False
    )
    assert verdict.confidence == "low"


def test_verdict_confidence_high_with_baseline_and_full_core():
    detectors = _clean_core_detectors()
    dims = build_dimensions(detectors, reliability=None)
    verdict = build_verdict(
        detectors, dims, profile_matched=True, used_judge=False, used_baseline=True
    )
    assert verdict.confidence == "high"
