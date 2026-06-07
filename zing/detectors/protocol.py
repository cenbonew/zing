"""Protocol detector — OpenAI-compatibility conformance.

A relay can return content yet still violate the OpenAI chat-completion contract:
forget prior turns, ignore ``stop``, omit ``finish_reason``/``usage``, or reject
malformed input with a non-standard error body. Each sub-check exercises one such
contract and contributes a 0-100 sub-score; the detector score is their average.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import contains_ci, usage_field
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status


@register
class ProtocolDetector(Detector):
    id = "protocol"
    name = "OpenAI-compatibility conformance"
    dimension = Dimension.PROTOCOL
    min_suite = "standard"
    cost_hint = 4

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        score_parts: list[float] = []

        score_parts.append(await self._check_multi_turn(ctx, result))
        score_parts.append(await self._check_stop_sequence(ctx, result))
        score_parts.append(await self._check_response_shape(ctx, result))
        score_parts.append(await self._check_error_schema(ctx, result))

        result.score = round(sum(score_parts) / len(score_parts), 1) if score_parts else None
        result.status = _roll_up_status(result.findings)
        return result

    # 1) Does the relay carry prior turns through to the model? ------------- #
    async def _check_multi_turn(self, ctx: AuditContext, result: DetectorResult) -> float:
        spec = RequestSpec(
            messages=[
                {"role": "system", "content": "Answer in one word."},
                {"role": "user", "content": "Remember the color: blue."},
                {"role": "assistant", "content": "OK, blue."},
                {"role": "user", "content": "What color did I ask you to remember?"},
            ],
            temperature=0.0,
            max_tokens=16,
        )
        outcome = await ctx.client.complete(spec)
        if not (outcome.ok and outcome.has_content()):
            result.findings.append(
                Finding(
                    id="protocol.multi_turn",
                    title="Multi-turn memory check did not return content",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}; no content.",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            return 50.0

        recalled = contains_ci(outcome.content, "blue")
        result.findings.append(
            Finding(
                id="protocol.multi_turn",
                title="Multi-turn conversation memory",
                status=Status.PASS if recalled else Status.WARN,
                severity=Severity.INFO if recalled else Severity.MEDIUM,
                summary=(
                    "Prior turns were honored; the recalled color was returned."
                    if recalled
                    else "Response did not recall the color from earlier turns; "
                    "the relay may be dropping conversation history."
                ),
                evidence={
                    "recalled_blue": recalled,
                    "content_preview": outcome.content[:120],
                },
                recommendation=None
                if recalled
                else "Verify the relay forwards the full messages array to the model.",
            )
        )
        return 100.0 if recalled else 55.0

    # 2) Is the ``stop`` sequence actually applied? ------------------------- #
    async def _check_stop_sequence(self, ctx: AuditContext, result: DetectorResult) -> float:
        spec = RequestSpec(
            messages=[{"role": "user", "content": "Print exactly: alpha STOP beta"}],
            stop="STOP",
            temperature=0.0,
            max_tokens=32,
        )
        outcome = await ctx.client.complete(spec)
        if not (outcome.ok and outcome.has_content()):
            result.findings.append(
                Finding(
                    id="protocol.stop",
                    title="Stop-sequence check did not return content",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}; no content.",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            return 50.0

        has_alpha = contains_ci(outcome.content, "alpha")
        has_beta = contains_ci(outcome.content, "beta")
        if has_alpha and not has_beta:
            status, severity, score = Status.PASS, Severity.INFO, 100.0
            summary = "Output was truncated at the stop sequence as expected."
        elif has_beta:
            status, severity, score = Status.WARN, Severity.LOW, 60.0
            summary = "Text after the stop sequence ('beta') was present; stop was ignored."
        else:
            status, severity, score = Status.WARN, Severity.LOW, 70.0
            summary = "Could not confirm stop handling from the response text."
        result.findings.append(
            Finding(
                id="protocol.stop",
                title="Stop-sequence handling",
                status=status,
                severity=severity,
                summary=summary,
                evidence={
                    "contains_alpha": has_alpha,
                    "contains_beta": has_beta,
                    "finish_reason": outcome.finish_reason,
                    "content_preview": outcome.content[:120],
                },
            )
        )
        return score

    # 3) Does a normal call carry finish_reason + a typed usage object? ----- #
    async def _check_response_shape(self, ctx: AuditContext, result: DetectorResult) -> float:
        spec = RequestSpec(
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
            temperature=0.0,
            max_tokens=16,
        )
        outcome = await ctx.client.complete(spec)
        if not outcome.ok:
            result.findings.append(
                Finding(
                    id="protocol.shape",
                    title="Response-shape check failed to return",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.LOW,
                    summary=outcome.error_message or f"HTTP {outcome.status_code}.",
                    evidence={"status_code": outcome.status_code, "error_type": outcome.error_type},
                )
            )
            return 50.0

        has_finish = bool(outcome.finish_reason)
        prompt_tokens = usage_field(outcome.usage, "prompt_tokens", "input_tokens")
        completion_tokens = usage_field(outcome.usage, "completion_tokens", "output_tokens")
        total_tokens = usage_field(outcome.usage, "total_tokens")
        has_usage = (
            prompt_tokens is not None
            and completion_tokens is not None
            and total_tokens is not None
        )
        conformant = has_finish and has_usage
        result.findings.append(
            Finding(
                id="protocol.shape",
                title="Response envelope shape",
                status=Status.PASS if conformant else Status.WARN,
                severity=Severity.INFO if conformant else Severity.LOW,
                summary=(
                    "finish_reason and an integer usage object were present."
                    if conformant
                    else "Response is missing finish_reason and/or a complete integer usage object."
                ),
                evidence={
                    "finish_reason": outcome.finish_reason,
                    "has_usage": has_usage,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
            )
        )
        return 100.0 if conformant else 65.0

    # 4) Does a malformed request fail with an OpenAI-style error body? ----- #
    async def _check_error_schema(self, ctx: AuditContext, result: DetectorResult) -> float:
        spec = RequestSpec(messages=[])  # empty messages — deliberately invalid
        outcome = await ctx.client.complete(spec)

        status_code = outcome.status_code
        raw_error = outcome.raw_error
        conforming = isinstance(raw_error, dict) and "error" in raw_error
        # Any client-error (4xx) is a correct rejection of an invalid request — not
        # only the 400-422 subset. The error-body SHAPE is a softer secondary signal.
        is_4xx = status_code is not None and 400 <= status_code <= 499
        is_5xx = status_code is not None and 500 <= status_code <= 599
        accepted = outcome.ok or status_code == 200

        if is_4xx and conforming:
            status, severity, score = Status.PASS, Severity.INFO, 100.0
            summary = f"Invalid request rejected with HTTP {status_code} and an OpenAI-style error body."
        elif is_4xx:
            status, severity, score = Status.WARN, Severity.LOW, 80.0
            summary = (
                f"Invalid request was correctly rejected with HTTP {status_code}, but the "
                "body is not an OpenAI-style {'error': {...}} object."
            )
        elif accepted:
            status, severity, score = Status.FAIL, Severity.MEDIUM, 30.0
            summary = "Empty-messages request was accepted (2xx) instead of being rejected."
        elif is_5xx:
            status, severity, score = Status.FAIL, Severity.MEDIUM, 35.0
            summary = (
                f"Invalid request produced a server error (HTTP {status_code}) rather than a 4xx."
            )
        else:
            status, severity, score = Status.WARN, Severity.LOW, 55.0
            summary = (
                f"Invalid request produced an unexpected outcome (HTTP {status_code}); could not "
                "confirm OpenAI-style client-error handling."
            )
        result.findings.append(
            Finding(
                id="protocol.error_schema",
                title="Error response schema",
                status=status,
                severity=severity,
                summary=summary,
                evidence={
                    "status_code": status_code,
                    "conforming_error_body": conforming,
                    "raw_error_keys": sorted(raw_error.keys()) if isinstance(raw_error, dict) else None,
                    "accepted_invalid": accepted,
                },
                recommendation=None
                if score >= 100.0
                else "A conformant relay should reject invalid input with a 4xx and an "
                "{'error': {...}} body.",
            )
        )
        return score


def _roll_up_status(findings: list[Finding]) -> Status:
    """Worst-of roll-up across sub-checks, ignoring purely informational ones."""
    order = [Status.PASS, Status.INCONCLUSIVE, Status.WARN, Status.FAIL]
    worst = Status.PASS
    for finding in findings:
        if finding.status in order and order.index(finding.status) > order.index(worst):
            worst = finding.status
    return worst
