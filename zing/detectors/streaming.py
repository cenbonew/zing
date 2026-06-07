"""Streaming authenticity detector.

Checks whether the relay actually streams tokens or merely buffers the full
response and replays it as one or two chunks (``stream.fake-streaming``). True
token streaming shows many chunks, an early first token, and spread-out
inter-chunk gaps; a buffered fake collapses all of that into a single dump.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status
from zing.utils import stats

# Buffered-streaming signals (few chunks / late first token) only make sense once
# enough text was produced that genuine streaming would have spanned many chunks.
# Below this, a fast small model finishing a short reply looks the same as a buffer.
_MIN_BUFFERED_CHARS = 220


@register
class StreamingDetector(Detector):
    id = "streaming"
    name = "Streaming authenticity"
    dimension = Dimension.STREAMING
    min_suite = "standard"
    cost_hint = 1

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()

        spec = RequestSpec(
            messages=[
                {
                    "role": "user",
                    "content": "Write five separate sentences about the ocean, each on its own line.",
                }
            ],
            temperature=0.0,
            max_tokens=256,
            stream=True,
        )
        out = await ctx.client.complete(spec)

        # Total failure — can't assess streaming at all.
        if not out.ok:
            result.findings.append(
                Finding(
                    id="streaming.failed",
                    title="Streaming request failed",
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary=out.error_message or f"HTTP {out.status_code}",
                    evidence={"status_code": out.status_code, "error_type": out.error_type},
                    recommendation="Verify the relay supports stream=true for this model.",
                )
            )
            result.status = Status.FAIL
            result.score = 0.0
            return result

        content_len = len(out.content or "")
        buffered_signals: list[str] = []

        # Signal 1: substantial content delivered in <=2 chunks.
        if content_len >= _MIN_BUFFERED_CHARS and out.chunk_count <= 2:
            buffered_signals.append("few_chunks")
            result.findings.append(
                Finding(
                    id="streaming.few_chunks",
                    title="Response delivered in <=2 chunks",
                    status=Status.WARN,
                    severity=Severity.MEDIUM,
                    summary=(
                        f"{content_len} chars arrived in {out.chunk_count} chunk(s) "
                        "(buffered-then-chunked, not true token streaming)."
                    ),
                    evidence={"content_chars": content_len, "chunk_count": out.chunk_count},
                )
            )

        # Signal 2: first token arrives near the very end (response withheld). Only
        # meaningful when enough text was produced to expect multi-chunk streaming.
        if (
            content_len >= _MIN_BUFFERED_CHARS
            and out.ttft_ms is not None
            and out.duration_ms
            and out.duration_ms > 0
        ):
            ttft_ratio = out.ttft_ms / out.duration_ms
            if ttft_ratio > 0.9:
                pct = round(ttft_ratio * 100)
                buffered_signals.append("late_ttft")
                result.findings.append(
                    Finding(
                        id="streaming.late_ttft",
                        title="First token arrived late in the stream",
                        status=Status.WARN,
                        severity=Severity.MEDIUM,
                        summary=f"First token arrived at ~{pct}% of total time (buffered).",
                        evidence={
                            "ttft_ms": round(out.ttft_ms, 1),
                            "duration_ms": round(out.duration_ms, 1),
                            "ttft_ratio": round(ttft_ratio, 3),
                        },
                    )
                )

        # Signal 3: many chunks but all dumped together (near-zero, uniform gaps).
        deltas = [
            out.chunk_timings_ms[i] - out.chunk_timings_ms[i - 1]
            for i in range(1, len(out.chunk_timings_ms))
        ]
        if out.chunk_count >= 4 and deltas:
            cv = stats.coefficient_of_variation(deltas)
            mean_delta = stats.mean(deltas)
            if cv is not None and mean_delta is not None and cv < 0.1 and mean_delta < 2.0:
                buffered_signals.append("uniform_gaps")
                result.findings.append(
                    Finding(
                        id="streaming.uniform_gaps",
                        title="Inter-chunk gaps are uniform and near-zero",
                        status=Status.WARN,
                        severity=Severity.MEDIUM,
                        summary=(
                            f"{out.chunk_count} chunks with ~{mean_delta:.2f} ms mean gap "
                            f"and CV {cv:.3f} — chunks appear dumped together."
                        ),
                        evidence={
                            "chunk_count": out.chunk_count,
                            "mean_delta_ms": round(mean_delta, 3),
                            "delta_cv": round(cv, 3),
                        },
                    )
                )

        # Usage chunk: only flag when usage is expected (or KB profile unknown).
        expects_usage = ctx.profile is None or ctx.profile.model.usage_in_stream
        missing_usage = out.usage is None and expects_usage
        if missing_usage:
            result.findings.append(
                Finding(
                    id="streaming.no_usage",
                    title="No usage chunk in stream",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary="stream_options.include_usage produced no usage data in the stream.",
                    evidence={"usage_present": False},
                )
            )

        # Healthy streaming evidence.
        if not buffered_signals:
            result.findings.append(
                Finding(
                    id="streaming.healthy",
                    title="Streaming looks authentic",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=(
                        f"{out.chunk_count} chunks over {out.duration_ms:.0f} ms"
                        + (f", first token at {out.ttft_ms:.0f} ms" if out.ttft_ms is not None else "")
                        + "."
                    ),
                    evidence={
                        "chunk_count": out.chunk_count,
                        "duration_ms": round(out.duration_ms, 1) if out.duration_ms else None,
                        "ttft_ms": round(out.ttft_ms, 1) if out.ttft_ms is not None else None,
                    },
                )
            )

        # Score & status from collected signals.
        n_buffered = len(buffered_signals)
        if n_buffered >= 2:
            result.score = 40.0
            result.status = Status.WARN
        elif n_buffered == 1:
            result.score = 60.0
            result.status = Status.WARN
        elif missing_usage:
            result.score = 85.0
            result.status = Status.WARN
        else:
            result.score = 100.0
            result.status = Status.PASS

        result.evidence.update(
            {
                "chunk_count": out.chunk_count,
                "content_chars": content_len,
                "duration_ms": round(out.duration_ms, 1) if out.duration_ms else None,
                "ttft_ms": round(out.ttft_ms, 1) if out.ttft_ms is not None else None,
                "usage_present": out.usage is not None,
                "buffered_signals": buffered_signals,
            }
        )
        return result
