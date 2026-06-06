"""Transport & secret-handling signals — static checks plus one light call.

Black-box auditing cannot prove what a relay does with a key server-side, so this
detector sticks to what is observable: whether the transport is encrypted, whether
the relay leaks its upstream identity in response headers, and whether the API key
is ever reflected back to the caller. Stronger claims (prompt logging, shared
upstream keys) are flagged as out-of-band — only reliability/timing can hint at
them indirectly.
"""

from __future__ import annotations

from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import contains_ci, stable_marker
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status
from zing.utils.redact import REDACTED_KEY

# Response headers that reveal upstream/proxy identity. Presence is informational:
# it can corroborate a substitution finding but is not itself a failure. Matched
# case-insensitively; "*"-suffixed entries match by prefix (header families).
_REVEALING_HEADERS: tuple[str, ...] = (
    "server",
    "via",
    "x-powered-by",
    "x-upstream-",
    "x-litellm-",
    "openai-organization",
    "x-served-by",
    "x-proxy-",
    "cf-",
)


def _header_matches(name: str) -> bool:
    lname = name.lower()
    for marker in _REVEALING_HEADERS:
        if marker.endswith("-"):
            if lname.startswith(marker):
                return True
        elif lname == marker:
            return True
    return False


@register
class SecurityDetector(Detector):
    id = "security"
    name = "Transport & secret-handling signals"
    dimension = Dimension.SECURITY
    min_suite = "smoke"

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()
        base_url = ctx.target.base_url or ""
        is_https = base_url.strip().lower().startswith("https://")

        # 1) TLS: an http endpoint sends the bearer token in clear text.
        if is_https:
            result.findings.append(
                Finding(
                    id="security.tls",
                    title="Endpoint uses HTTPS",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary="Transport is encrypted; the API key is protected in transit.",
                    evidence={"scheme": "https"},
                )
            )
        else:
            result.findings.append(
                Finding(
                    id="security.tls",
                    title="Endpoint is not HTTPS",
                    status=Status.FAIL,
                    severity=Severity.HIGH,
                    summary="Endpoint is not HTTPS; the API key is sent in clear text.",
                    evidence={"scheme": base_url.split("://", 1)[0].lower() if "://" in base_url else ""},
                    recommendation="Use an https:// base_url so the bearer token is not exposed on the wire.",
                )
            )

        # 2) + 3) One small chat call funds both the header-hygiene and key-echo
        # checks. Headers reaching us are already redacted by the client.
        marker = stable_marker("security")
        spec = RequestSpec(
            messages=[
                {"role": "user", "content": f"Reply with exactly this text and nothing else: {marker}"}
            ],
            temperature=0.0,
            max_tokens=32,
        )
        chat = await ctx.client.complete(spec)

        # 2) Header hygiene — purely informational.
        if chat.headers:
            revealing = {k: v for k, v in chat.headers.items() if _header_matches(k)}
            if revealing:
                result.findings.append(
                    Finding(
                        id="security.headers",
                        title="Response leaks upstream/proxy headers",
                        status=Status.INFO,
                        severity=Severity.LOW,
                        summary=(
                            f"Found {len(revealing)} revealing header(s): "
                            f"{', '.join(sorted(revealing))}. "
                            "Informational — can corroborate the upstream identity, not a failure."
                        ),
                        evidence={"revealing_headers": revealing},
                    )
                )
            else:
                result.findings.append(
                    Finding(
                        id="security.headers",
                        title="No revealing upstream headers",
                        status=Status.PASS,
                        severity=Severity.INFO,
                        summary=f"Inspected {len(chat.headers)} response headers; none expose upstream identity.",
                        evidence={"header_count": len(chat.headers)},
                    )
                )
        else:
            result.findings.append(
                Finding(
                    id="security.headers",
                    title="No response headers to inspect",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.INFO,
                    summary=chat.error_message or "Call returned no headers; header hygiene not assessed.",
                    evidence={"status_code": chat.status_code, "error_type": chat.error_type},
                )
            )

        # 3) Secret echo — the key must not appear verbatim in returned text.
        # The client scrubs the configured key to REDACTED_KEY before any relay text
        # reaches us, so the raw key never lands in a report. We detect an echo by the
        # presence of that sentinel; for an atypically short key (not pattern-scrubbed)
        # we fall back to a direct comparison.
        key = ctx.target.api_key or ""
        key_echoed = False
        if key:
            haystacks = [chat.content or "", chat.error_message or ""]
            key_echoed = any(REDACTED_KEY in text for text in haystacks)
            if not key_echoed and len(key) < 6:
                key_echoed = any(contains_ci(text, key) for text in haystacks if text)
            if key_echoed:
                result.findings.append(
                    Finding(
                        id="security.key_echo",
                        title="API key reflected in response",
                        status=Status.FAIL,
                        severity=Severity.HIGH,
                        summary="The API key appears verbatim in the relay's response; treat the key as exposed.",
                        evidence={"location": "content_or_error"},
                        recommendation="Rotate the key and avoid this relay echoing credentials.",
                    )
                )
            else:
                result.findings.append(
                    Finding(
                        id="security.key_echo",
                        title="API key not reflected in response",
                        status=Status.PASS,
                        severity=Severity.INFO,
                        summary="The API key does not appear in the response content or error text.",
                        evidence={},
                    )
                )

        # 4) Limits of black-box inspection — no score impact.
        result.findings.append(
            Finding(
                id="security.note",
                title="Prompt logging & shared upstream keys are not black-box provable",
                status=Status.INFO,
                severity=Severity.INFO,
                summary=(
                    "Whether a relay logs prompts or multiplexes a shared upstream key cannot be "
                    "verified from the client side. Treat the reliability and streaming-timing "
                    "dimensions as indirect signals (e.g. cross-request latency coupling)."
                ),
                evidence={},
            )
        )

        # SCORE: 100 healthy https with no key echo; 40 for http (key on the wire);
        # a verbatim key echo is the dominant transport-security failure.
        if not is_https:
            result.score = 40.0
            result.status = Status.FAIL
        elif key_echoed:
            result.score = 30.0
            result.status = Status.FAIL
        else:
            result.score = 100.0
            result.status = Status.PASS

        result.evidence["https"] = is_https
        result.evidence["key_present"] = bool(key)
        return result
