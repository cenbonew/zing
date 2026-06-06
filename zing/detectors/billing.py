"""Token/usage billing audit.

Sends one bounded request with a fixed, known-size prompt and compares the
relay's reported ``usage`` against an independent token estimate. The estimate is
heuristic (exact only when a tiktoken encoding is available), so we tolerate wide
margins and flag only gross deviations — inflated counts that would overbill the
buyer, missing accounting that makes billing unverifiable, or internally
inconsistent totals.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import usage_field
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status
from zing.utils.tokenize import estimate_messages_tokens, estimate_tokens

# A fixed paragraph (~110 words) so the prompt size is stable across runs.
_KNOWN_PARAGRAPH = (
    "The municipal water authority published its annual report on Tuesday, "
    "outlining a decade-long plan to modernize aging pipelines across the "
    "northern districts. Engineers warned that several mains, installed in the "
    "early twentieth century, had begun to corrode and leak, wasting an "
    "estimated fifteen percent of treated water before it reached homes. The "
    "proposed budget allocates funds for sensor networks that detect pressure "
    "drops in real time, allowing crews to locate ruptures within hours rather "
    "than days. Residents at the public hearing voiced concern about rate "
    "increases, while council members emphasized that deferring repairs would "
    "ultimately cost far more in emergency excavation and water loss over time."
)


@register
class BillingDetector(Detector):
    id = "billing"
    name = "Token/usage billing audit"
    dimension = Dimension.BILLING
    min_suite = "standard"

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        tok = ctx.tokenizer_hint()

        spec = RequestSpec(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{_KNOWN_PARAGRAPH}\n\n"
                        "Summarize the text above in exactly two sentences."
                    ),
                }
            ],
            temperature=0.0,
            max_tokens=120,
        )
        outcome = await ctx.client.complete(spec)

        # Transport-level failure: nothing to bill-check, degrade to INCONCLUSIVE.
        if not (outcome.ok and outcome.has_content()):
            result.findings.append(
                Finding(
                    id="billing.request-failed",
                    title="Billing probe request failed",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            result.status = Status.INCONCLUSIVE
            result.score = None
            return result

        est_prompt = estimate_messages_tokens(spec.messages, tok)
        est_completion = estimate_tokens(outcome.content, tok)

        usage = outcome.usage
        prompt = usage_field(usage, "prompt_tokens", "input_tokens")
        completion = usage_field(usage, "completion_tokens", "output_tokens")
        total = usage_field(usage, "total_tokens")

        ratio_prompt = (prompt / est_prompt) if (prompt is not None and est_prompt > 0) else None
        ratio_completion = (
            (completion / est_completion) if (completion is not None and est_completion > 0) else None
        )

        result.evidence = {
            "reported": {"prompt": prompt, "completion": completion, "total": total},
            "estimated": {"prompt": est_prompt, "completion": est_completion},
            "ratio_prompt": round(ratio_prompt, 3) if ratio_prompt is not None else None,
            "ratio_completion": round(ratio_completion, 3) if ratio_completion is not None else None,
            "tokenizer": tok,
            "estimate_note": (
                "Token estimate uses a heuristic fallback; treat as ~±25% unless an "
                "exact tiktoken encoding was available."
            ),
        }

        # No usage block at all — billing cannot be independently verified.
        if prompt is None and completion is None and total is None:
            result.findings.append(
                Finding(
                    id="billing.missing-usage",
                    title="No usage accounting returned",
                    status=Status.WARN,
                    severity=Severity.MEDIUM,
                    summary="Response omitted token usage; billing is unverifiable.",
                    evidence={"usage_present": usage is not None},
                    recommendation="Confirm with the provider how usage is metered if billing is per-token.",
                )
            )
            result.status = Status.WARN
            result.score = 75.0
            return result

        score = 100.0
        worst = Status.PASS

        # Prompt-token inflation (the buyer-harmful direction).
        if prompt is not None and prompt > 1.8 * est_prompt and prompt > est_prompt + 50:
            result.findings.append(
                Finding(
                    id="billing.usage-inflation",
                    title="Reported prompt tokens far exceed estimate",
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary=(
                        f"Reported prompt tokens ({prompt}) far exceed independent "
                        f"estimate (~{est_prompt})."
                    ),
                    evidence={"reported_prompt": prompt, "estimated_prompt": est_prompt, "ratio": ratio_prompt},
                    recommendation="Cross-check billing against a known-size prompt; possible per-token overbilling.",
                )
            )
            score = min(score, 55.0)
            worst = Status.FAIL

        # Completion-token inflation, judged the same way.
        if completion is not None and completion > 1.8 * est_completion and completion > est_completion + 50:
            result.findings.append(
                Finding(
                    id="billing.usage-inflation-completion",
                    title="Reported completion tokens far exceed estimate",
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary=(
                        f"Reported completion tokens ({completion}) far exceed "
                        f"independent estimate (~{est_completion})."
                    ),
                    evidence={
                        "reported_completion": completion,
                        "estimated_completion": est_completion,
                        "ratio": ratio_completion,
                    },
                    recommendation="Cross-check billing against output length; possible per-token overbilling.",
                )
            )
            score = min(score, 55.0)
            worst = Status.FAIL

        # Severe under-count: cheaper for the buyer, so only an informational note.
        for label, reported, estimated in (
            ("prompt", prompt, est_prompt),
            ("completion", completion, est_completion),
        ):
            if reported is not None and estimated > 0 and reported < 0.5 * estimated:
                result.findings.append(
                    Finding(
                        id=f"billing.usage-undercount-{label}",
                        title=f"Reported {label} tokens well below estimate",
                        status=Status.INFO,
                        severity=Severity.INFO,
                        summary=(
                            f"Reported {label} tokens ({reported}) are far below the "
                            f"estimate (~{estimated}); not buyer-harmful but unusual."
                        ),
                        evidence={f"reported_{label}": reported, f"estimated_{label}": estimated},
                    )
                )

        # Internal consistency of the reported total.
        if (
            total is not None
            and prompt is not None
            and completion is not None
            and abs(total - (prompt + completion)) > 2
        ):
            result.findings.append(
                Finding(
                    id="billing.total-mismatch",
                    title="Usage total != prompt + completion",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary=(
                        f"Reported total ({total}) does not equal prompt "
                        f"({prompt}) + completion ({completion})."
                    ),
                    evidence={"total": total, "prompt": prompt, "completion": completion},
                )
            )
            score = min(score, 90.0)
            if worst == Status.PASS:
                worst = Status.WARN

        # Within tolerance and accounted for — but only if the per-direction
        # breakdown is actually present. A response that reports only total_tokens
        # (prompt/completion missing) cannot be confirmed "consistent": the split
        # billing depends on was never validated.
        if worst == Status.PASS:
            if prompt is None or completion is None:
                result.findings.append(
                    Finding(
                        id="billing.partial-usage",
                        title="Incomplete usage breakdown",
                        status=Status.WARN,
                        severity=Severity.MEDIUM,
                        summary=(
                            f"Usage reports only total={total} (prompt={prompt}, "
                            f"completion={completion}); the per-direction split that "
                            "per-token billing depends on is missing and unverifiable."
                        ),
                        evidence={"reported_prompt": prompt, "reported_completion": completion, "reported_total": total},
                        recommendation="Confirm with the provider how prompt/completion tokens are metered.",
                    )
                )
                worst = Status.WARN
                score = min(score, 80.0)
            else:
                result.findings.append(
                    Finding(
                        id="billing.usage-consistent",
                        title="Usage consistent with independent estimate",
                        status=Status.PASS,
                        severity=Severity.INFO,
                        summary=(
                            f"Reported prompt={prompt}, completion={completion} within "
                            f"tolerance of estimate (~{est_prompt}/~{est_completion})."
                        ),
                        evidence={"ratio_prompt": ratio_prompt, "ratio_completion": ratio_completion},
                    )
                )

        result.status = worst
        result.score = round(score, 1)
        return result
