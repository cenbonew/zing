"""LLM-judged quality / downgrade assessment — the flagship code+LLM hybrid.

Poses a small battery of tier-discriminating prompts (multi-step reasoning, a
precise coding task, nuanced instruction-following) to the target, optionally
mirrors them against a trusted baseline, then asks a *separate trusted judge*
whether the target's answers read like the model it claims to be or like a
cheaper / quantized / substituted stand-in. The judge never sees credentials —
only the model answers.
"""

from __future__ import annotations

from typing import Any

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.models import (
    CompletionOutcome,
    DetectorResult,
    Dimension,
    Finding,
    RequestSpec,
    Severity,
    Status,
)

# Discriminating prompts: each separates capable models from cheaper ones along a
# different axis. Kept short and deterministic so answers are comparable.
_PROBES: tuple[tuple[str, str], ...] = (
    (
        "reasoning",
        "A train leaves city A at 9:00 AM traveling 60 km/h toward city B, 210 km "
        "away. A second train leaves city B at 9:30 AM traveling 90 km/h toward "
        "city A on the same track. At what clock time do they meet, and how far "
        "from city A? Show each step of your reasoning, then give the final answer.",
    ),
    (
        "coding",
        "Write a Python function `merge_intervals(intervals)` that merges all "
        "overlapping closed integer intervals given as a list of [start, end] "
        "pairs and returns them sorted by start. Handle empty input and "
        "touching-but-not-overlapping intervals correctly. Include a one-line "
        "complexity note. Return only the code and the note.",
    ),
    (
        "instruction",
        "In exactly three sentences, explain why floating-point addition is not "
        "associative, and include the phrase 'rounding error' exactly once. Do not "
        "use any bullet points or numbered lists.",
    ),
)

_ANSWER_CLIP = 2000  # cap each answer fed to the judge, keeping the prompt bounded


@register
class QualityJudgeDetector(Detector):
    id = "quality_judge"
    name = "LLM-judged quality / downgrade assessment"
    dimension = Dimension.MODEL_IDENTITY
    min_suite = "deep"
    requires_judge = True
    cost_hint = 5

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        result.used_judge = True
        claimed = ctx.target.model

        # 1) Gather target (and optional baseline) answers to the probe battery.
        target_answers: list[dict[str, Any]] = []
        baseline_answers: list[dict[str, Any]] = []
        target_failures = 0

        for label, prompt in _PROBES:
            outcome = await self._ask(ctx.client, prompt)
            answer = self._extract(outcome)
            if answer is None:
                target_failures += 1
            target_answers.append({"label": label, "prompt": prompt, "answer": answer or ""})

            if ctx.has_baseline and ctx.baseline_client is not None:
                b_outcome = await self._ask(ctx.baseline_client, prompt)
                baseline_answers.append(
                    {"label": label, "answer": self._extract(b_outcome) or ""}
                )

        # If the target could not answer any probe, there is nothing to judge.
        if target_failures == len(_PROBES):
            result.findings.append(
                Finding(
                    id="quality_judge.unavailable",
                    title="No target answers to judge",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary="Target returned no usable content for any discriminating probe.",
                    evidence={"probes": len(_PROBES), "failed": target_failures},
                    recommendation="Re-run once the endpoint reliably returns completions.",
                )
            )
            result.status = Status.INCONCLUSIVE
            result.score = None
            return result

        # 2) Build the judge prompt from model answers only (no keys / no base_url).
        system = (
            f"You are an expert LLM evaluator assessing whether an API endpoint that "
            f"CLAIMS to be '{claimed}' actually behaves like it, or like a "
            f"cheaper/quantized/substituted model. Judge quality, depth, reasoning, "
            f"and style. Be calibrated and cautious."
        )
        user = self._build_user_prompt(claimed, target_answers, baseline_answers, ctx)

        assert ctx.judge is not None  # requires_judge=True guarantees a judge here
        verdict = await ctx.judge.evaluate_json(system, user, max_tokens=700)

        # 3) Interpret the verdict robustly — any malformed/error shape is inconclusive.
        kind = str(verdict.get("verdict", "")).strip().lower()
        confidence = str(verdict.get("confidence", "")).strip().lower()
        likely_tier = verdict.get("likely_actual_tier")
        reasoning = verdict.get("reasoning")
        judge_errored = "_error" in verdict

        evidence: dict[str, Any] = {
            "claimed_model": claimed,
            "probe_labels": [p["label"] for p in target_answers],
            "target_probe_failures": target_failures,
            "baseline_compared": bool(baseline_answers),
            "judge_verdict": kind or None,
            "judge_confidence": confidence or None,
            "likely_actual_tier": likely_tier,
            "judge_reasoning": reasoning,
        }
        if judge_errored:
            evidence["judge_error"] = verdict.get("_error")
        result.evidence.update(evidence)

        if kind == "suspicious":
            low_conf = confidence in ("low", "medium")
            # A lone judge opinion is a single fuzzy signal from one trusted model and
            # must not, by itself, reach the strongest "does not deliver the claimed
            # model" verdict (RiskLevel.HIGH). Only escalate to HIGH severity when a
            # trusted BASELINE corroborated the side-by-side AND confidence is high;
            # otherwise cap at MEDIUM so it needs a second core signal to drive HIGH.
            corroborated = bool(baseline_answers)
            high_sev = (not low_conf) and corroborated
            result.findings.append(
                Finding(
                    id="quality_judge.suspicious",
                    title=(
                        "LLM judge: behavior inconsistent with claimed model "
                        "(possible downgrade/substitution/quantization)"
                    ),
                    status=Status.FAIL if high_sev else Status.WARN,
                    severity=Severity.HIGH if high_sev else Severity.MEDIUM,
                    summary=(
                        f"Judge ({confidence or 'unspecified'} confidence) found the answers "
                        f"unlike '{claimed}'"
                        + (f"; likely closer to: {likely_tier}." if likely_tier else ".")
                        + ("" if corroborated else " No trusted baseline was compared, so this "
                           "stays a corroboration-required signal — run `zing compare`.")
                    ),
                    evidence={
                        "judge_confidence": confidence or None,
                        "likely_actual_tier": likely_tier,
                        "reasoning": reasoning,
                        "baseline_corroborated": corroborated,
                    },
                    recommendation=(
                        "Treat as a divergence signal, not proof; corroborate with "
                        "fingerprint and identity detectors before acting."
                    ),
                )
            )
            result.status = Status.FAIL if high_sev else Status.WARN
            result.score = 25.0 if confidence == "high" else 50.0
        elif kind == "consistent":
            result.findings.append(
                Finding(
                    id="quality_judge.consistent",
                    title="LLM judge: behavior consistent with claimed model",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=(
                        f"Judge ({confidence or 'unspecified'} confidence) found the answers "
                        f"consistent with '{claimed}'."
                    ),
                    evidence={"judge_confidence": confidence or None, "reasoning": reasoning},
                )
            )
            result.status = Status.PASS
            result.score = 95.0
        else:
            # "inconclusive", an unrecognized verdict, or a judge error.
            result.findings.append(
                Finding(
                    id="quality_judge.inconclusive",
                    title="LLM judge: assessment inconclusive",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=(
                        verdict.get("_error")
                        or f"Judge could not commit to a verdict for '{claimed}'."
                    ),
                    evidence={"judge_confidence": confidence or None, "reasoning": reasoning},
                )
            )
            result.status = Status.INCONCLUSIVE
            result.score = None

        return result

    # -- internals --------------------------------------------------------- #
    async def _ask(self, client, prompt: str) -> CompletionOutcome:
        """Issue one probe at temp 0; never raise on a misbehaving relay."""
        spec = RequestSpec(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=400,
        )
        try:
            return await client.complete(spec)
        except Exception as exc:  # defensive: client should not raise, but a relay may surprise us
            return CompletionOutcome(ok=False, error_message=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _extract(outcome: CompletionOutcome) -> str | None:
        if outcome.ok and outcome.has_content():
            return outcome.content.strip()[:_ANSWER_CLIP]
        return None

    @staticmethod
    def _build_user_prompt(
        claimed: str,
        target_answers: list[dict[str, Any]],
        baseline_answers: list[dict[str, Any]],
        ctx: AuditContext,
    ) -> str:
        lines: list[str] = []
        family = ctx.profile.model.family if ctx.profile else None
        cutoff = ctx.profile.model.knowledge_cutoff if ctx.profile else None
        ctx_bits = [f"claimed model id: {claimed}"]
        if family:
            ctx_bits.append(f"expected family: {family}")
        if cutoff:
            ctx_bits.append(f"declared knowledge cutoff: {cutoff}")
        lines.append("CONTEXT — " + "; ".join(ctx_bits) + ".")
        lines.append("")
        lines.append(
            "Below are answers from the endpoint UNDER AUDIT (the 'TARGET') to a "
            "battery of discriminating prompts"
            + (", alongside a TRUSTED BASELINE for side-by-side comparison" if baseline_answers else "")
            + ". Assess whether the TARGET answers reflect the capability, depth, "
            "and style expected of the claimed model."
        )

        baseline_by_label = {b["label"]: b["answer"] for b in baseline_answers}
        for i, item in enumerate(target_answers, start=1):
            lines.append("")
            lines.append(f"### Prompt {i} [{item['label']}]")
            lines.append(item["prompt"])
            lines.append("")
            lines.append("--- TARGET answer ---")
            lines.append(item["answer"] or "(no answer returned)")
            if item["label"] in baseline_by_label:
                lines.append("")
                lines.append("--- TRUSTED BASELINE answer ---")
                lines.append(baseline_by_label[item["label"]] or "(no answer returned)")

        lines.append("")
        lines.append(
            "Return ONLY a JSON object with these exact keys: "
            '{"verdict":"consistent|suspicious|inconclusive",'
            '"confidence":"low|medium|high",'
            '"likely_actual_tier":"<your best guess at the real model/tier, or unknown>",'
            '"reasoning":"<concise justification grounded in the answers>"}. '
            "Use 'suspicious' only when the answers are materially weaker, shallower, "
            "or stylistically off versus what the claimed model should produce. When "
            "unsure, prefer 'inconclusive'."
        )
        return "\n".join(lines)
