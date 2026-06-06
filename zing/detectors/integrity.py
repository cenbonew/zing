"""Response-integrity / tampering detector.

A malicious proxy can rewrite content in flight — typosquatting an install URL or
package name, swapping a domain — while leaving everything else intact. zing sends
known-answer canaries ("output exactly this line") whose values are sensitive
(a URL, a pinned package) and checks whether the structure comes back with the
value SUBSTITUTED.

It is deliberately precise to avoid false positives: an exact echo is PASS, a
non-echo (the model didn't comply / paraphrased) is INCONCLUSIVE, and only a
*structure-preserved value substitution* is treated as tampering. A lone
substitution caps at MEDIUM; in ``compare`` mode, a substitution the trusted
baseline does NOT make is escalated to CRITICAL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import contains_ci
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status


@dataclass
class _Canary:
    id: str
    prompt: str
    expected: str                 # exact substring that must survive intact
    pattern: re.Pattern           # matches the structure (group(0)) and value(s)
    correct: str                  # the canonical value the structure should carry

    def substitution(self, text: str) -> str | None:
        """Return a description if the structure is present but the value changed."""
        m = self.pattern.search(text or "")
        if not m:
            return None
        found = m.group("val")
        if found == self.correct:
            return None
        return f"{self.id}: expected {self.correct!r}, got {found!r}"


_CANARIES: tuple[_Canary, ...] = (
    _Canary(
        id="install_url",
        prompt=(
            "Output exactly this line and nothing else:\n"
            "ZING_URL=https://downloads.zing-audit.test/safe/setup.sh"
        ),
        expected="https://downloads.zing-audit.test/safe/setup.sh",
        pattern=re.compile(r"ZING_URL=https?://(?P<val>[^/\s\"']+)"),
        correct="downloads.zing-audit.test",
    ),
    _Canary(
        id="package_pin",
        prompt="Output exactly this and nothing else: pip install zing-audit==0.1.0",
        expected="zing-audit==0.1.0",
        pattern=re.compile(r"pip install (?P<val>[A-Za-z0-9._-]+==[0-9][0-9A-Za-z.\-]*)"),
        correct="zing-audit==0.1.0",
    ),
)


@register
class IntegrityDetector(Detector):
    id = "integrity"
    name = "Response integrity / tampering"
    dimension = Dimension.SECURITY
    min_suite = "deep"

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        substitutions: list[str] = []
        verbatim = 0
        noncompliant = 0

        for canary in _CANARIES:
            text = await self._ask(ctx, canary.prompt)
            if text is None:
                noncompliant += 1
                continue
            if contains_ci(text, canary.expected):
                verbatim += 1
                continue
            sub = canary.substitution(text)
            if sub:
                # In compare mode, only count it if the trusted baseline kept it intact.
                if ctx.has_baseline:
                    base = await self._ask(ctx, canary.prompt, baseline=True)
                    if base is not None and not contains_ci(base, canary.expected):
                        # Baseline also failed to echo it — likely model behavior, not tampering.
                        noncompliant += 1
                        continue
                substitutions.append(sub)
            else:
                noncompliant += 1

        result.evidence.update(
            {"substitutions": substitutions, "verbatim": verbatim, "noncompliant": noncompliant}
        )

        if substitutions:
            corroborated = ctx.has_baseline
            result.findings.append(
                Finding(
                    id="integrity.tampering",
                    title="Relay-controlled value substitution detected",
                    status=Status.FAIL,
                    severity=Severity.CRITICAL if corroborated else Severity.MEDIUM,
                    summary=(
                        "A known-answer canary came back with its value substituted while "
                        "its structure was preserved"
                        + (
                            " — and the trusted baseline returned it intact, strong evidence "
                            "of in-flight tampering."
                            if corroborated
                            else ". Treat as a strong signal; run `zing compare` against a "
                            "trusted baseline to confirm."
                        )
                    ),
                    evidence={"substitutions": substitutions, "baseline_corroborated": corroborated},
                    recommendation="A proxy rewriting URLs/package names is a supply-chain risk — stop using it.",
                )
            )
            result.status = Status.FAIL
            result.score = 10.0 if corroborated else 45.0
        elif verbatim:
            result.findings.append(
                Finding(
                    id="integrity.intact",
                    title="Known-answer canaries returned intact",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=f"{verbatim} canary value(s) echoed verbatim; no substitution observed.",
                    evidence={"verbatim": verbatim, "noncompliant": noncompliant},
                )
            )
            result.status = Status.PASS
            result.score = 100.0
        else:
            result.findings.append(
                Finding(
                    id="integrity.inconclusive",
                    title="Integrity canaries inconclusive",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.INFO,
                    summary="The model did not echo any canary verbatim, so tampering could not be assessed.",
                    evidence={"noncompliant": noncompliant},
                )
            )
            result.status = Status.INCONCLUSIVE
            result.score = None
        return result

    async def _ask(self, ctx: AuditContext, prompt: str, *, baseline: bool = False) -> str | None:
        client = ctx.baseline_client if baseline else ctx.client
        if client is None:
            return None
        outcome = await client.complete(
            RequestSpec(messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=80)
        )
        if outcome.ok and outcome.has_content():
            return outcome.content
        return None
