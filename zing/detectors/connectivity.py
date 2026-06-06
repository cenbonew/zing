"""Connectivity detector — the canonical example all detectors mirror.

Checks the two most basic things: is the endpoint reachable, and does a plain
chat completion for the claimed model actually return content. Everything else
builds on this passing.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import contains_ci, stable_marker
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status


@register
class ConnectivityDetector(Detector):
    id = "connectivity"
    name = "Connectivity & basic completion"
    dimension = Dimension.CONNECTIVITY
    min_suite = "smoke"

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        score_parts: list[float] = []

        # 1) /models reachability (secondary — some relays disable it).
        models_outcome, model_ids = await ctx.client.list_models()
        if models_outcome.ok:
            claimed_listed = any(ctx.target.model == mid for mid in model_ids)
            result.findings.append(
                Finding(
                    id="connectivity.models",
                    title="/v1/models reachable",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=(
                        f"Listed {len(model_ids)} models; claimed model "
                        f"{'is' if claimed_listed else 'is NOT'} present."
                    ),
                    evidence={"model_count": len(model_ids), "claimed_listed": claimed_listed},
                )
            )
            score_parts.append(100.0)
        else:
            result.findings.append(
                Finding(
                    id="connectivity.models",
                    title="/v1/models not reachable",
                    status=Status.WARN,
                    severity=Severity.LOW,
                    summary=models_outcome.error_message or f"HTTP {models_outcome.status_code}",
                    evidence={"status_code": models_outcome.status_code},
                )
            )
            score_parts.append(60.0)

        # 2) Basic chat completion with an exact canary (most important signal).
        marker = stable_marker("connectivity")
        spec = RequestSpec(
            messages=[
                {"role": "user", "content": f"Reply with exactly this text and nothing else: {marker}"}
            ],
            temperature=0.0,
            max_tokens=32,
        )
        chat = await ctx.client.complete(spec)
        if chat.ok and chat.has_content():
            recalled = contains_ci(chat.content, marker)
            result.findings.append(
                Finding(
                    id="connectivity.chat",
                    title="Basic chat completion works",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=f"Returned content in {chat.duration_ms:.0f} ms; canary {'echoed' if recalled else 'not echoed'}.",
                    evidence={
                        "duration_ms": chat.duration_ms,
                        "model_returned": chat.model_returned,
                        "canary_echoed": recalled,
                    },
                )
            )
            score_parts.append(100.0 if recalled else 85.0)
            result.status = Status.PASS
        else:
            result.findings.append(
                Finding(
                    id="connectivity.chat",
                    title="Basic chat completion failed",
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary=chat.error_message or f"HTTP {chat.status_code}",
                    evidence={"status_code": chat.status_code, "error_type": chat.error_type},
                    recommendation="Verify base_url, api_key, and that the model id is served.",
                )
            )
            score_parts.append(0.0)
            result.status = Status.FAIL

        result.score = round(sum(score_parts) / len(score_parts), 1) if score_parts else None
        result.evidence["model_ids_sample"] = model_ids[:20]
        return result
