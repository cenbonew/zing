"""Audit orchestration: wire up context, run detectors, assemble the report.

Detectors run sequentially against a single endpoint on purpose — concurrent
hammering would trip rate limits and confound the latency/streaming timing
measurements. The reliability detector does its own bounded concurrency
internally.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path

import zing.detectors  # noqa: F401  -- populates the detector REGISTRY
from zing import __version__
from zing.clients import OpenAICompatibleClient
from zing.config import AuditOptions
from zing.context import AuditContext
from zing.detectors.base import run_detector, select_detectors
from zing.judge import Judge
from zing.knowledge import load_knowledge_base
from zing.models import (
    AuditReport,
    DetectorResult,
    Dimension,
    RedactedTarget,
    ReliabilitySummary,
    TargetConfig,
)
from zing.scoring import build_dimensions, build_verdict
from zing.utils.redact import fingerprint_secret


def _redact(config: TargetConfig) -> RedactedTarget:
    return RedactedTarget(
        name=config.name,
        kind=config.kind,
        base_url=config.base_url,
        model=config.model,
        declared_provider=config.declared_provider,
        api_key_fingerprint=fingerprint_secret(config.api_key),
    )


def _extract_reliability(detectors: list[DetectorResult]) -> ReliabilitySummary | None:
    for det in detectors:
        if det.dimension == Dimension.RELIABILITY:
            payload = det.evidence.get("reliability")
            if isinstance(payload, dict):
                try:
                    return ReliabilitySummary(**payload)
                except Exception:
                    return None
    return None


async def run_audit(
    target: TargetConfig,
    options: AuditOptions,
    *,
    baseline: TargetConfig | None = None,
    judge_target: TargetConfig | None = None,
    mode: str = "check",
    command: str | None = None,
    kb_dirs: list[Path] | None = None,
) -> AuditReport:
    kb = load_knowledge_base(kb_dirs)
    profile = kb.resolve(target.model, target.declared_provider)

    async with AsyncExitStack() as stack:
        client = await stack.enter_async_context(OpenAICompatibleClient(target))

        baseline_client = None
        if baseline is not None:
            baseline_client = await stack.enter_async_context(
                OpenAICompatibleClient(baseline)
            )

        judge = None
        if options.judge:
            jt = judge_target or baseline
            if jt is not None:
                judge_client = await stack.enter_async_context(
                    OpenAICompatibleClient(jt)
                )
                judge = Judge(judge_client, jt.model)

        ctx = AuditContext(
            target=target,
            client=client,
            options=options,
            kb=kb,
            profile=profile,
            baseline=baseline,
            baseline_client=baseline_client,
            judge=judge,
        )

        detectors = select_detectors(
            options.suite,
            has_judge=judge is not None,
            has_baseline=baseline_client is not None,
            enabled=options.enabled,
        )
        results: list[DetectorResult] = []
        for detector in detectors:
            results.append(await run_detector(detector, ctx))

    reliability = _extract_reliability(results)
    dimensions = build_dimensions(results, reliability)
    verdict = build_verdict(
        results,
        dimensions,
        profile_matched=profile is not None,
        used_judge=judge is not None,
        used_baseline=baseline_client is not None,
    )

    warnings = [
        f"{d.id}: {d.error}" for d in results if d.error
    ]
    notes = [
        "zing performs black-box auditing: it gathers reproducible evidence of "
        "behavioral divergence, not cryptographic proof of model identity.",
        "Use `zing compare` against a trusted baseline of the same declared model "
        "for the strongest downgrade evidence.",
        "Do not publish a report that names a vendor without reviewing sample size, "
        "cost, and local law/policy.",
    ]

    judge_endpoint = judge_target or baseline
    return AuditReport(
        tool_version=__version__,
        mode=mode,
        generated_at=datetime.now(timezone.utc).isoformat(),
        command=command,
        suite=options.suite,
        target=_redact(target),
        baseline=_redact(baseline) if baseline else None,
        verdict=verdict,
        dimensions=dimensions,
        detectors=results,
        reliability=reliability,
        judge_used=judge is not None,
        judge_model=judge_endpoint.model if (options.judge and judge_endpoint is not None) else None,
        notes=notes,
        warnings=warnings,
    )
