"""Reliability detector — concurrent success rate & latency under load.

Fires a burst of identical tiny requests with bounded concurrency and measures
how many succeed and how fast they return. A relay that flakes, rate-limits, or
crawls under modest parallelism is a reliability risk even when single calls
look fine. Aggregated into a :class:`ReliabilitySummary` the runner surfaces.
"""

from __future__ import annotations

import asyncio

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.models import (
    DetectorResult,
    Dimension,
    Finding,
    ReliabilitySummary,
    RequestSpec,
    Severity,
    Status,
)
from zing.utils.stats import summarize

# A p95 above this (ms) is slow enough to flag and dampen the score.
_SLOW_P95_MS = 30_000.0


@register
class ReliabilityDetector(Detector):
    id = "reliability"
    name = "Concurrent reliability & latency"
    dimension = Dimension.RELIABILITY
    min_suite = "standard"
    cost_hint = 8

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()

        n = ctx.options.reliability_requests
        if n <= 0:
            result.status = Status.NOT_RUN
            result.score = None
            result.findings.append(
                Finding(
                    id="reliability.skipped",
                    title="Reliability probe disabled",
                    status=Status.INFO,
                    severity=Severity.INFO,
                    summary="reliability_requests <= 0; no concurrent load was issued.",
                    evidence={"reliability_requests": n},
                )
            )
            return result

        conc = max(1, ctx.options.reliability_concurrency)
        spec = RequestSpec(
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=0.0,
            max_tokens=8,
        )

        # Fire n identical requests, capping in-flight calls with a semaphore.
        sem = asyncio.Semaphore(conc)

        async def _one() -> tuple[bool, float | None, str | None, int | None]:
            async with sem:
                outcome = await ctx.client.complete(spec)
            return outcome.ok, outcome.duration_ms, outcome.error_type, outcome.status_code

        results = await asyncio.gather(*(_one() for _ in range(n)))

        successes = 0
        rate_limited = 0
        durations: list[float] = []
        errors: dict[str, int] = {}
        for ok, duration_ms, error_type, status_code in results:
            if ok:
                successes += 1
                if duration_ms is not None:
                    durations.append(duration_ms)
            elif status_code == 429:
                # The relay correctly throttling the burst is honest behavior, not
                # instability — keep it out of the success-rate denominator.
                rate_limited += 1
            else:
                key = error_type or "unknown"
                errors[key] = errors.get(key, 0) + 1

        # Success rate is over genuinely-attempted (non-throttled) requests.
        effective = n - rate_limited
        success_rate = successes / effective if effective > 0 else 1.0
        latency = summarize(durations)
        summary = ReliabilitySummary(
            requests=n,
            successes=successes,
            success_rate=success_rate,
            rate_limited=rate_limited,
            latency_ms=latency,
            errors=errors,
        )
        result.evidence["reliability"] = summary.model_dump()

        p95 = latency.get("p95")
        slow = p95 is not None and p95 > _SLOW_P95_MS
        failed = effective - successes

        # Headline finding: how the burst fared (over genuinely-attempted requests).
        if effective == 0:
            result.findings.append(
                Finding(
                    id="reliability.success_rate",
                    title="All concurrent requests were rate-limited",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=f"All {n} requests returned HTTP 429 at concurrency {conc}; "
                    "reliability under load could not be assessed.",
                    evidence={"requests": n, "rate_limited": rate_limited, "concurrency": conc},
                    recommendation="Lower --concurrency or --reliability-requests and re-run.",
                )
            )
        elif successes == effective:
            result.findings.append(
                Finding(
                    id="reliability.success_rate",
                    title="All concurrent requests succeeded",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=f"{successes} of {effective} attempted requests succeeded at "
                    f"concurrency {conc}"
                    + (f" ({rate_limited} rate-limited)." if rate_limited else "."),
                    evidence={
                        "requests": n,
                        "successes": successes,
                        "rate_limited": rate_limited,
                        "concurrency": conc,
                        "success_rate": round(success_rate, 4),
                    },
                )
            )
        else:
            severe = success_rate < 0.9
            result.findings.append(
                Finding(
                    id="reliability.success_rate",
                    title="Some concurrent requests failed",
                    status=Status.WARN if not severe else Status.FAIL,
                    severity=Severity.MEDIUM if severe else Severity.LOW,
                    summary=f"{failed} of {effective} attempted requests failed at "
                    f"concurrency {conc}"
                    + (f" ({rate_limited} rate-limited, excluded)." if rate_limited else "."),
                    evidence={
                        "requests": n,
                        "successes": successes,
                        "rate_limited": rate_limited,
                        "concurrency": conc,
                        "success_rate": round(success_rate, 4),
                        "errors": errors,
                    },
                    recommendation=(
                        "Check the relay's connection pool and upstream stability under "
                        "parallel load (HTTP 429 throttling is excluded from this rate)."
                    ),
                )
            )

        # Throttling is honest behavior — surface it, but only as information.
        if rate_limited and effective > 0:
            result.findings.append(
                Finding(
                    id="reliability.rate_limited",
                    title="Relay rate-limited part of the burst",
                    status=Status.INFO,
                    severity=Severity.INFO,
                    summary=f"{rate_limited} of {n} requests returned HTTP 429 at concurrency "
                    f"{conc}; counted as throttling, not instability.",
                    evidence={"rate_limited": rate_limited, "requests": n, "concurrency": conc},
                )
            )

        # Latency finding: only when we have data and p95 is high.
        if slow:
            result.findings.append(
                Finding(
                    id="reliability.latency",
                    title="High tail latency under load",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary=f"p95 latency {p95:.0f} ms exceeds {_SLOW_P95_MS:.0f} ms.",
                    evidence={"latency_ms": latency, "concurrency": conc},
                )
            )

        if effective == 0:
            # Nothing but throttling — no reliability signal to score.
            result.score = None
            result.status = Status.INCONCLUSIVE
            return result

        score = round(success_rate * 100, 1)
        if slow:
            score = round(score * 0.85, 1)
        result.score = score
        if score >= 85:
            result.status = Status.PASS
        elif score >= 70:
            result.status = Status.WARN
        else:
            result.status = Status.FAIL
        return result
