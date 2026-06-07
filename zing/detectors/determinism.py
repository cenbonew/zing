"""Determinism & cache-correctness detector.

A relay that secretly caches responses to cut inference cost will return the
same text for an identical prompt regardless of sampling parameters. The tell is
sampling at ``temperature=1.0``: a genuine model should vary; byte-identical
output is a caching signal. Identity at ``temperature=0`` is expected and not
penalized — deterministic decoding is correct behavior there.

To avoid falsely accusing honest models, the caching call only fires when ALL of
several long high-temperature samples come back byte-identical, and it is
suppressed for reasoning models, which legitimately ignore/clamp sampling
parameters (e.g. deepseek-reasoner ignores temperature) and can repeat output.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status


@register
class DeterminismDetector(Detector):
    id = "determinism"
    name = "Determinism & cache-correctness"
    dimension = Dimension.PROTOCOL
    min_suite = "deep"
    cost_hint = 6

    # Number of high-temperature samples that must ALL be byte-identical before a
    # caching finding fires. More than two so a chance collision on a short output
    # can't trip it.
    CACHING_SAMPLES = 4

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        score_parts: list[float] = []
        # Reasoning models legitimately ignore/clamp temperature and may repeat
        # output, so a byte-identical run is NOT evidence of caching for them.
        reasoning = bool(ctx.profile and getattr(ctx.profile.model, "reasoning", False))

        # 1) High-temperature variability — the load-bearing caching probe. A longer
        # generation has far more entropy, so genuine sampling almost never repeats.
        creative = RequestSpec(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Write an original six-line free-verse poem about the sea at "
                        "dawn. Make unexpected word choices."
                    ),
                }
            ],
            temperature=1.0,
            max_tokens=220,
        )
        outcomes = [await ctx.client.complete(creative) for _ in range(self.CACHING_SAMPLES)]
        usable = [o for o in outcomes if o.ok and o.has_content()]

        if len(usable) >= 2:
            texts = [o.content.strip() for o in usable]
            all_identical = len(set(texts)) == 1
            previews = {f"output_{i}_preview": t[:160] for i, t in enumerate(texts[:3])}
            if all_identical and not reasoning:
                result.findings.append(
                    Finding(
                        id="determinism.temp1_variability",
                        title="Identical output at temperature=1.0 suggests response caching",
                        status=Status.WARN,
                        severity=Severity.MEDIUM,
                        summary=(
                            f"{len(usable)} identical long creative prompts at "
                            "temperature=1.0 (no seed) returned byte-identical text. A "
                            "genuine sampling model should vary; this is consistent with a "
                            "relay serving cached responses."
                        ),
                        evidence={
                            "temperature": 1.0,
                            "samples": len(usable),
                            "identical": True,
                            "model_returned": usable[0].model_returned,
                            **previews,
                        },
                        recommendation=(
                            "Confirm with varied prompts; a cache that ignores sampling "
                            "parameters can mask the served model's true behavior."
                        ),
                    )
                )
                score_parts.append(55.0)
            elif all_identical and reasoning:
                result.findings.append(
                    Finding(
                        id="determinism.temp1_variability",
                        title="Identical output at temperature=1.0 (expected for a reasoning model)",
                        status=Status.INFO,
                        severity=Severity.INFO,
                        summary=(
                            f"{len(usable)} samples were byte-identical, but the claimed "
                            "model is a reasoning model that legitimately ignores sampling "
                            "parameters — not treated as a caching signal."
                        ),
                        evidence={"temperature": 1.0, "samples": len(usable), "reasoning": True},
                    )
                )
                score_parts.append(100.0)
            else:
                result.findings.append(
                    Finding(
                        id="determinism.temp1_variability",
                        title="Output varies at temperature=1.0",
                        status=Status.PASS,
                        severity=Severity.INFO,
                        summary=(
                            f"{len(usable)} identical creative prompts at temperature=1.0 "
                            "produced differing text, as expected for genuine sampling."
                        ),
                        evidence={"temperature": 1.0, "samples": len(usable), "identical": False, **previews},
                    )
                )
                score_parts.append(100.0)
        else:
            # A relay that errors or returns nothing cannot be judged for caching.
            failed = next((o for o in outcomes if not (o.ok and o.has_content())), outcomes[0])
            result.findings.append(
                Finding(
                    id="determinism.temp1_variability",
                    title="Could not assess temperature=1.0 variability",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=failed.error_message or f"HTTP {failed.status_code}",
                    evidence={
                        "usable_samples": len(usable),
                        "requested_samples": self.CACHING_SAMPLES,
                        "status_code": failed.status_code,
                        "error_type": failed.error_type,
                    },
                )
            )

        # 2) Determinism sanity at temperature=0 — informational only.
        factual = RequestSpec(
            messages=[{"role": "user", "content": "Name the capital of France in one word."}],
            temperature=0.0,
            max_tokens=16,
        )
        third = await ctx.client.complete(factual)
        fourth = await ctx.client.complete(factual)

        if third.ok and third.has_content() and fourth.ok and fourth.has_content():
            ans_a = third.content.strip()
            ans_b = fourth.content.strip()
            stable = ans_a == ans_b
            result.findings.append(
                Finding(
                    id="determinism.temp0_stability",
                    title=(
                        "Stable answer at temperature=0"
                        if stable
                        else "Answer drifts at temperature=0"
                    ),
                    status=Status.INFO,
                    severity=Severity.INFO,
                    summary=(
                        "Repeated factual prompt at temperature=0 returned "
                        + ("identical text (expected)." if stable else "differing text.")
                    ),
                    evidence={
                        "temperature": 0.0,
                        "identical": stable,
                        "answer_a": ans_a[:120],
                        "answer_b": ans_b[:120],
                    },
                )
            )
        else:
            failed = third if not (third.ok and third.has_content()) else fourth
            result.findings.append(
                Finding(
                    id="determinism.temp0_stability",
                    title="Could not assess temperature=0 stability",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=failed.error_message or f"HTTP {failed.status_code}",
                    evidence={
                        "third_ok": third.ok and third.has_content(),
                        "fourth_ok": fourth.ok and fourth.has_content(),
                        "status_code": failed.status_code,
                        "error_type": failed.error_type,
                    },
                )
            )

        # Verdict: caching signal at temp=1 is the only thing that lowers status.
        if score_parts:
            result.score = round(sum(score_parts) / len(score_parts), 1)
            caching = any(f.id == "determinism.temp1_variability" and f.status == Status.WARN
                          for f in result.findings)
            result.status = Status.WARN if caching else Status.PASS
        else:
            result.score = None
            result.status = Status.INCONCLUSIVE
        return result
