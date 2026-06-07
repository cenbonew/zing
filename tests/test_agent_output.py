"""Tests for the agent/LLM-facing CLI surface: compact JSON, dry-run plan, and
per-detector cost hints."""

from __future__ import annotations

import json

import zing.detectors  # noqa: F401  -- populate the registry
from zing.cli import _build_dry_run_plan
from zing.config import AuditOptions
from zing.detectors.base import REGISTRY
from zing.models import (
    AuditReport,
    DetectorResult,
    Dimension,
    Finding,
    RedactedTarget,
    Severity,
    Status,
    TargetConfig,
)
from zing.report import compact_dict, render_compact, render_json
from zing.scoring import build_dimensions, build_verdict


def _report() -> AuditReport:
    detectors = [
        DetectorResult(
            id="model_identity-det", name="identity", dimension=Dimension.MODEL_IDENTITY,
            score=20.0, status=Status.FAIL,
            findings=[
                Finding(
                    id="model_identity.self_id", title="Self-identifies as a rival brand",
                    status=Status.FAIL, severity=Severity.HIGH,
                    summary="Says it is Doubao by ByteDance.",
                    recommendation="Substitution suspected.",
                    evidence={"target_answer": "I am Doubao " + "x" * 600},  # bulky
                ),
                Finding(
                    id="model_identity.ok", title="Fingerprint consistent",
                    status=Status.PASS, severity=Severity.INFO,
                    evidence={"observed": "y" * 600},
                ),
            ],
        ),
        DetectorResult(
            id="context_window-det", name="ctx", dimension=Dimension.CONTEXT_WINDOW,
            score=90.0, status=Status.PASS, findings=[],
        ),
    ]
    dims = build_dimensions(detectors, None)
    verdict = build_verdict(detectors, dims, profile_matched=True, used_judge=False, used_baseline=False)
    return AuditReport(
        tool_version="0.0.0", mode="check", generated_at="t", suite="standard",
        target=RedactedTarget(name="t", kind="target", base_url="https://relay.test/v1", model="deepseek-v4-flash"),
        verdict=verdict, dimensions=dims, detectors=detectors,
    )


def test_compact_is_lean_and_well_shaped():
    report = _report()
    compact = render_compact(report)
    full = render_json(report)

    # Much smaller, and the bulky evidence strings are gone.
    assert len(compact) < len(full)
    assert "x" * 600 not in compact
    assert "y" * 600 not in compact

    c = json.loads(compact)
    assert c["tool"] == "zing"
    assert c["verdict"]["risk"] in ("clean", "low", "medium", "high", "inconclusive")
    assert "model_identity" in c["dimensions"]
    # Actionable findings keep a summary; pass/info findings stay one line.
    fail = next(f for f in c["findings"] if f["id"] == "model_identity.self_id")
    assert fail["severity"] == "high" and "summary" in fail
    ok = next(f for f in c["findings"] if f["id"] == "model_identity.ok")
    assert "summary" not in ok


def test_compact_dict_keys_stable():
    c = compact_dict(_report())
    for key in ("tool", "version", "mode", "suite", "target", "verdict", "dimensions", "findings"):
        assert key in c


def test_dry_run_plan_estimates_without_running():
    plan = _build_dry_run_plan(
        TargetConfig(base_url="https://relay.test/v1", model="gpt-4o"),
        AuditOptions(suite="deep"), None, None, "check",
    )
    assert plan["dry_run"] is True
    assert plan["estimated_api_calls"] > 0
    assert plan["detectors"] and all("est_calls" in d for d in plan["detectors"])
    assert plan["estimated_api_calls"] == sum(d["est_calls"] for d in plan["detectors"])


def test_every_detector_has_a_cost_hint():
    for det_id, cls in REGISTRY.items():
        assert isinstance(cls.cost_hint, int), det_id
        assert cls.cost_hint >= 1, det_id
