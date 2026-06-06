"""Real context window & truncation detector — the headline check.

Measures the largest prompt the relay can actually ingest and recall a needle
from, and compares it to the advertised context window. A relay that silently
truncates, summarizes, or RAG-shims long prompts will recall well below its
claim, or fail a needle buried in the middle while passing start and end. zing
reports the measured divergence; it never asserts fraud outright.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import build_haystack, contains_ci, stable_marker
from zing.models import (
    CompletionOutcome,
    DetectorResult,
    Dimension,
    Finding,
    RequestSpec,
    Severity,
    Status,
)

# A 4xx whose body mentions one of these context-specific phrases is the relay
# rejecting the prompt for SIZE — strong evidence of where the real ceiling sits.
# Kept deliberately narrow: bare "length"/"token"/"max_tokens" also appear in
# parameter-validation errors (see _PARAM_REJECTION_HINTS), which must NOT be read
# as a context ceiling or an honest reasoning model would be flagged truncated.
_CONTEXT_ERROR_HINTS = (
    "context length",
    "context_length",
    "context window",
    "context_length_exceeded",
    "maximum context",
    "exceeds the context",
    "reduce the length of the messages",
    "too many tokens",
)

# A 4xx complaining about a request PARAMETER (not the prompt size). Reasoning
# models reject "max_tokens" and require "max_completion_tokens"; that is a
# parameter mismatch, not a context truncation, and must stay inconclusive.
_PARAM_REJECTION_HINTS = (
    "max_tokens",
    "max_completion_tokens",
    "unsupported parameter",
    "unsupported value",
    "unknown parameter",
    "is not supported",
    "use 'max_completion_tokens'",
)


@register
class ContextWindowDetector(Detector):
    id = "context_window"
    name = "Real context window & truncation"
    dimension = Dimension.CONTEXT_WINDOW
    min_suite = "deep"

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        tokenizer = ctx.tokenizer_hint()

        declared = ctx.declared_context_window()
        cap = ctx.options.max_context_probe_tokens
        floor = ctx.options.context_probe_floor_tokens
        upper = min(declared, cap) if declared else cap

        ladder_sizes = self._ladder(floor, upper)
        if not ladder_sizes:
            result.findings.append(
                Finding(
                    id="context_window.no_ladder",
                    title="Context-window probe could not run",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=f"No probe sizes fit between floor and cap (upper={upper}).",
                    evidence={"declared": declared, "upper": upper, "floor": floor},
                )
            )
            result.status = Status.INCONCLUSIVE
            result.score = None
            return result

        # Reasoning models reject max_tokens and require max_completion_tokens; probe
        # them accordingly so a 400 isn't misread as a context ceiling.
        reasoning = bool(ctx.profile and getattr(ctx.profile.model, "reasoning", False))

        # --- Ascending ladder: find the largest recalling size and the first
        # failing/rejected size above it. Stop after the first failure. Each size is
        # probed at an EDGE depth and a miss is confirmed before it counts. -------- #
        ladder_log: list[dict] = []
        calls = 0
        effective_window: int | None = None
        last_pass: int | None = None
        first_fail: int | None = None
        rejected_at: int | None = None

        for size in ladder_sizes:
            recalled, rejected, status, n = await self._probe_size(
                ctx, size, tokenizer=tokenizer, reasoning=reasoning
            )
            calls += n
            ladder_log.append(
                {"size": size, "recalled": recalled, "rejected": rejected, "status": status}
            )
            if recalled:
                last_pass = size
                effective_window = size
                continue
            # First non-recall (a clean fail, a size rejection, or an odd error).
            first_fail = size
            if rejected:
                rejected_at = size
            break

        # --- One binary-search refine between last pass and first failure. ---- #
        if last_pass is not None and first_fail is not None and first_fail > last_pass:
            mid = (last_pass + first_fail) // 2
            if last_pass < mid < first_fail:
                recalled, rejected, status, n = await self._probe_size(
                    ctx, mid, tokenizer=tokenizer, reasoning=reasoning
                )
                calls += n
                ladder_log.append(
                    {
                        "size": mid,
                        "recalled": recalled,
                        "rejected": rejected,
                        "status": status,
                        "refine": True,
                    }
                )
                if recalled:
                    last_pass = mid
                    effective_window = mid
                elif rejected and rejected_at is None:
                    rejected_at = mid

        # --- Lost-in-the-middle at a mid size (depths 0.1 / 0.5 / 0.9). ------- #
        depth_results: dict[str, bool | None] = {}
        lost_in_middle = False
        mid_size = min(32_000, effective_window or 16_000)
        # Only worth spending calls if the mid size is at/below what we know works.
        if mid_size >= floor and (effective_window is None or mid_size <= effective_window):
            for depth in (0.1, 0.5, 0.9):
                outcome = await self._probe(
                    ctx, mid_size, depth=depth, tokenizer=tokenizer, reasoning=reasoning
                )
                calls += 1
                recalled, _, _ = self._classify(outcome, mid_size)
                depth_results[f"{depth:.1f}"] = recalled
            start_ok = depth_results.get("0.1") is True
            end_ok = depth_results.get("0.9") is True
            middle_ok = depth_results.get("0.5") is True
            if start_ok and end_ok and not middle_ok:
                lost_in_middle = True
                result.findings.append(
                    Finding(
                        id="context_window.lost_in_middle",
                        title="Needle in the middle was not recalled",
                        status=Status.WARN,
                        severity=Severity.MEDIUM,
                        summary=(
                            "Start and end of the prompt were recalled but the middle "
                            "was not (lost-in-the-middle; cheap RAG/summarization shim "
                            "suspected)."
                        ),
                        evidence={"mid_size": mid_size, "depth_results": depth_results},
                        recommendation="Compare against a trusted baseline to confirm.",
                    )
                )

        # --- Verdicts ---------------------------------------------------------- #
        if rejected_at is not None and declared and rejected_at < 0.9 * declared:
            result.findings.append(
                Finding(
                    id="context_window.rejected_below_claim",
                    title="Endpoint rejects context below its advertised window",
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary=(
                        f"A ~{rejected_at}-token prompt was rejected with a context-length "
                        f"error, well below the declared {declared}-token window."
                    ),
                    evidence={"declared": declared, "rejected_at": rejected_at},
                    recommendation="Treat the advertised context window as unverified.",
                )
            )

        if declared:
            measured = effective_window or 0
            ratio = measured / declared if declared else 0.0
            if effective_window is None:
                # Even the smallest ladder rung did not recall — nothing usable.
                result.findings.append(
                    Finding(
                        id="context_window.no_recall",
                        title="No probe size recalled the needle",
                        status=Status.FAIL,
                        severity=Severity.HIGH,
                        summary=(
                            f"Even a ~{ladder_sizes[0]}-token prompt failed recall; the "
                            f"usable window appears far below the declared {declared}."
                        ),
                        evidence={"declared": declared, "smallest_probed": ladder_sizes[0]},
                    )
                )
                result.status = Status.FAIL
            elif ratio < 0.5:
                result.findings.append(
                    Finding(
                        id="context_window.truncation",
                        title="Real context window far below the declared one",
                        status=Status.FAIL,
                        severity=Severity.HIGH,
                        summary=(
                            f"Real context window ~{measured} << declared {declared} "
                            f"(silent truncation suspected)."
                        ),
                        evidence={"declared": declared, "effective_window": measured, "ratio": round(ratio, 3)},
                        recommendation="The relay likely truncates or summarizes long prompts.",
                    )
                )
                result.status = Status.FAIL
            elif ratio < 0.9:
                result.findings.append(
                    Finding(
                        id="context_window.short",
                        title="Real context window below the declared one",
                        status=Status.WARN,
                        severity=Severity.MEDIUM,
                        summary=(
                            f"Real context window ~{measured} is below the declared "
                            f"{declared} (recall degrades before the advertised limit)."
                        ),
                        evidence={"declared": declared, "effective_window": measured, "ratio": round(ratio, 3)},
                    )
                )
                result.status = Status.WARN if result.status != Status.FAIL else result.status
            else:
                result.findings.append(
                    Finding(
                        id="context_window.consistent",
                        title="Measured context window consistent with the claim",
                        status=Status.PASS,
                        severity=Severity.INFO,
                        summary=(
                            f"Recalled a needle at ~{measured} tokens, near the declared "
                            f"{declared}."
                        ),
                        evidence={"declared": declared, "effective_window": measured, "ratio": round(ratio, 3)},
                    )
                )
                if result.status not in (Status.FAIL, Status.WARN):
                    result.status = Status.PASS

            score = round(min(1.0, ratio) * 100, 1)
            if lost_in_middle:
                score = round(max(0.0, score - 15.0), 1)
            result.score = score
        else:
            # Declared window unknown — report the measurement, do not score.
            result.findings.append(
                Finding(
                    id="context_window.measured",
                    title="Measured effective context window",
                    status=Status.INFO,
                    severity=Severity.INFO,
                    summary=(
                        f"No declared context window to compare against; measured "
                        f"effective window ~{effective_window or '<' + str(ladder_sizes[0])} tokens."
                    ),
                    evidence={"declared": None, "effective_window": effective_window},
                )
            )
            result.status = Status.INFO
            result.score = None

        result.evidence.update(
            {
                "declared": declared,
                "effective_window": effective_window,
                "ladder": ladder_log,
                "depth_results": depth_results,
                "calls": calls,
            }
        )
        return result

    # -- internals ---------------------------------------------------------- #
    @staticmethod
    def _ladder(floor: int, upper: int) -> list[int]:
        """Ascending doubling ladder up to ``upper``, plus a ~0.9*upper rung.

        Capped at 7 sizes so the ascending sweep stays within budget.
        """
        if upper <= 0:
            return []
        start = max(floor, 2_000)
        if start > upper:
            start = upper
        sizes: list[int] = []
        size = start
        while size <= upper:
            sizes.append(size)
            size *= 2
        near_top = int(upper * 0.9)
        if near_top > 0 and near_top not in sizes and (not sizes or near_top > sizes[-1]):
            sizes.append(near_top)
        # Dedup, sort, and keep the largest 7 distinct rungs in ascending order.
        sizes = sorted({s for s in sizes if s > 0})
        if len(sizes) > 7:
            sizes = sizes[-7:]
        return sizes

    async def _probe_size(
        self, ctx: AuditContext, size: int, *, tokenizer: str | None, reasoning: bool
    ) -> tuple[bool, bool, str, int]:
        """Decide recall at ``size`` from the most-recalled EDGE positions.

        Returns ``(recalled, rejected_for_size, status, calls)``. A single missed
        needle is not enough to declare a size failed (a benign paraphrase or a
        dropped marker would falsely shrink the window and drive a HIGH truncation
        verdict): a miss is confirmed at a second edge before it counts as a fail.
        A clear size-rejection is authoritative on the first hit.
        """
        calls = 0
        last_status = "no_recall"
        for depth in (0.95, 0.0):
            outcome = await self._probe(
                ctx, size, depth=depth, tokenizer=tokenizer, reasoning=reasoning
            )
            calls += 1
            recalled, rejected, status = self._classify(outcome, size)
            last_status = status
            if recalled:
                return True, False, status, calls
            if rejected:
                return False, True, status, calls
            # else non-recall / param_error / transport error — try the other edge.
        return False, False, last_status, calls

    async def _probe(
        self,
        ctx: AuditContext,
        size: int,
        *,
        depth: float,
        tokenizer: str | None,
        reasoning: bool = False,
    ) -> CompletionOutcome:
        needle = stable_marker(f"ctxwin-{size}-{depth}")
        haystack = build_haystack(
            total_tokens=size, needle=needle, depth=depth, tokenizer=tokenizer
        )
        messages = [{"role": "user", "content": haystack}]
        outcome = await self._call(ctx, messages, use_completion_tokens=reasoning)
        # A non-reasoning probe that gets a max_tokens parameter rejection is very
        # likely an unprofiled reasoning model — retry once with max_completion_tokens
        # rather than misreading the 4xx as a context ceiling.
        if not reasoning and self._is_param_rejection(outcome):
            outcome = await self._call(ctx, messages, use_completion_tokens=True)
        # Stash the needle so the caller's classifier can check recall.
        outcome.headers.setdefault("x-zing-needle", needle)
        return outcome

    @staticmethod
    async def _call(
        ctx: AuditContext, messages: list[dict], *, use_completion_tokens: bool
    ) -> CompletionOutcome:
        if use_completion_tokens:
            spec = RequestSpec(
                messages=messages,
                temperature=0.0,
                max_tokens=None,
                extra_body={"max_completion_tokens": 64},
            )
        else:
            spec = RequestSpec(messages=messages, temperature=0.0, max_tokens=64)
        return await ctx.client.complete(spec)

    @staticmethod
    def _classify(outcome: CompletionOutcome, size: int) -> tuple[bool, bool, str]:
        """Return (recalled, rejected_for_size, status_label).

        ``recalled`` is True only on a successful response echoing the needle.
        ``rejected_for_size`` is True for a 400..422 whose error text reads like a
        context/length rejection — the relay declining the prompt at this size.
        """
        needle = outcome.headers.get("x-zing-needle", "")
        if outcome.ok and outcome.has_content() and needle:
            if contains_ci(outcome.content, needle):
                return True, False, "recalled"
            return False, False, "no_recall"

        code = outcome.status_code
        if code is not None and 400 <= code <= 422:
            text = (outcome.error_message or "").lower()
            # Context-size rejection takes priority: a clear context phrase is a real
            # ceiling even if the message also names max_tokens.
            if any(hint in text for hint in _CONTEXT_ERROR_HINTS):
                return False, True, "rejected"
            # A bare parameter rejection (e.g. "max_tokens is not supported, use
            # max_completion_tokens") is NOT a size ceiling — stay inconclusive.
            if any(hint in text for hint in _PARAM_REJECTION_HINTS):
                return False, False, "param_error"
            return False, False, "http_error"
        # Timeouts, 5xx, malformed/empty responses — treat as a non-recall failure
        # but not a deliberate size rejection.
        return False, False, "error"

    @staticmethod
    def _is_param_rejection(outcome: CompletionOutcome) -> bool:
        """True if a 4xx is rejecting the max_tokens parameter (not the prompt size)."""
        code = outcome.status_code
        if code is None or not (400 <= code <= 422):
            return False
        text = (outcome.error_message or "").lower()
        if any(hint in text for hint in _CONTEXT_ERROR_HINTS):
            return False
        return any(hint in text for hint in _PARAM_REJECTION_HINTS)
