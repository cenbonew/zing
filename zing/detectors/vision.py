"""Multimodal (vision) capability verification detector.

A relay can claim a vision-capable model yet quietly route to a text-only
substitute that simply *says* it can see, or that hallucinates an answer. This
detector settles the question with a known-answer image probe: it generates a
small solid-color PNG at runtime (stdlib only — no Pillow), sends it inline as a
data URI alongside a one-word color question, and checks whether the model names
the color it could only know by actually looking.

The image is a single distinctive, hard-to-blind-guess color (orange,
RGB 255,140,0): not a primary color a text-only model could land on by chance,
and not the canonical "red/green/blue" a guesser reaches for first. A text-only
model that cannot see the bytes has no way to produce "orange" reliably.

Verdict is deliberately false-positive averse. Correct color -> PASS (vision
delivered). A refusal / no color / wrong color -> WARN (MEDIUM): the claimed
vision is not being delivered, possibly a text-only substitute. Transport error
or empty response -> INCONCLUSIVE. This check never escalates to HIGH on its own;
one image is suggestive, not conclusive proof of substitution.

Budget: a single chat completion.
"""

from __future__ import annotations

import base64
import struct
import zlib

from zing.clients import detect_api
from zing.context import AuditContext
from zing.detectors.base import Detector, register
from zing.detectors.helpers import contains_any
from zing.models import DetectorResult, Dimension, Finding, RequestSpec, Severity, Status
from zing.utils.redact import redact_text

# The known-answer color. Orange (255,140,0) is intentionally non-primary: a
# text-only model guessing blind is far likelier to say red/green/blue/black/white
# than to land on "orange". Accept English + Chinese synonyms so an honest
# vision model answering in either language passes.
_COLOR_RGB = (255, 140, 0)
_EXPECTED_COLORS = ("orange", "橙", "橘")  # 橙色 / 橘色 / 橘黄 all contain these

_QUESTION = (
    "仅用一个词回答：图片是什么颜色？/ In one word, what color is this image?"
)

# Hints that the model is admitting it cannot actually process the image. If the
# answer carries one of these and no expected color, the claimed vision is a
# text-only substitute rather than a wrong guess.
_BLIND_PHRASES = (
    "can't see", "cannot see", "can not see", "unable to see", "not able to see",
    "can't view", "cannot view", "unable to view",
    "don't see", "do not see", "no image", "without an image", "no picture",
    "can't process", "cannot process", "unable to process",
    "as a text", "text-based", "text-only", "language model", "i'm unable",
    "无法看到", "看不到", "无法查看", "没有图片", "无法处理图像", "无法识别图",
    "我是一个文本", "纯文本", "无法看图",
)


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Build a ``width`` x ``height`` solid-color PNG using only the stdlib.

    Encodes a truecolor (RGB, 8-bit) image: signature + IHDR + a single zlib-
    deflated IDAT of filtered scanlines + IEND, each chunk CRC-checked.
    """

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + body + struct.pack(">I", crc)

    r, g, b = rgb
    # Each scanline: 1 filter byte (0 = none) + RGB triplets across the row.
    scanline = b"\x00" + bytes((r, g, b)) * width
    raw = scanline * height
    signature = b"\x89PNG\r\n\x1a\n"
    # IHDR: width, height, bit depth 8, color type 2 (truecolor), default codecs.
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, 9)
    return (
        signature
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", idat)
        + _chunk(b"IEND", b"")
    )


def _image_message(api: str, png_bytes: bytes) -> dict:
    """Build a single user message carrying the image + question per protocol.

    ``openai``   -> image_url part + {"type":"text"} part
    ``responses``-> input_image part + {"type":"input_text"} part
    ``anthropic``-> image/base64 source part + {"type":"text"} part
    """
    b64 = base64.b64encode(png_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64}"

    if api == "anthropic":
        image_part = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64},
        }
        text_part = {"type": "text", "text": _QUESTION}
    elif api == "responses":
        image_part = {"type": "input_image", "image_url": data_uri}
        text_part = {"type": "input_text", "text": _QUESTION}
    else:  # openai (Chat Completions) and anything else default to this shape
        image_part = {"type": "image_url", "image_url": {"url": data_uri}}
        text_part = {"type": "text", "text": _QUESTION}

    return {"role": "user", "content": [image_part, text_part]}


@register
class VisionDetector(Detector):
    id = "vision"
    name = "Multimodal (vision) capability verification"
    dimension = Dimension.CAPABILITY
    min_suite = "deep"
    cost_hint = 1

    async def run(self, ctx: AuditContext) -> DetectorResult:
        result = self.new_result()

        modalities = list(ctx.profile.model.modalities) if ctx.profile else []
        claims_vision = bool(ctx.profile) and (
            "vision" in modalities or "image" in modalities
        )

        # Only meaningful when the claimed model is supposed to see images.
        if not claims_vision:
            result.findings.append(
                Finding(
                    id="vision.not_claimed",
                    title="Vision not claimed — skipped",
                    status=Status.INFO,
                    severity=Severity.INFO,
                    summary=(
                        "The resolved profile does not claim a vision/image modality; "
                        "no multimodal probe was sent."
                    ),
                    evidence={"has_profile": bool(ctx.profile), "modalities": modalities},
                )
            )
            result.status = Status.INFO
            result.score = None
            return result

        png = _solid_png(64, 64, _COLOR_RGB)
        api = detect_api(ctx.target)
        message = _image_message(api, png)

        outcome = await ctx.client.complete(
            RequestSpec(messages=[message], temperature=0.0, max_tokens=32)
        )

        evidence_base = {
            "protocol": api,
            "expected_colors": list(_EXPECTED_COLORS),
            "image": f"{_COLOR_RGB} solid 64x64 PNG (data URI)",
        }

        # Transport failure or empty response: we learned nothing about vision.
        if not (outcome.ok and outcome.has_content()):
            result.findings.append(
                Finding(
                    id="vision.color",
                    title="Vision probe inconclusive",
                    status=Status.INCONCLUSIVE,
                    severity=Severity.INFO,
                    summary=(
                        outcome.error_message
                        or f"No usable response (HTTP {outcome.status_code})."
                    ),
                    evidence={
                        **evidence_base,
                        "status_code": outcome.status_code,
                        "error_type": outcome.error_type,
                    },
                )
            )
            result.status = Status.INCONCLUSIVE
            result.score = None
            return result

        # Content is already redacted by the client; truncate for the report.
        answer = redact_text(outcome.content, extra_secrets=[ctx.target.api_key])
        observed = answer.strip()[:300]
        saw_color = contains_any(answer, list(_EXPECTED_COLORS))
        admitted_blind = contains_any(answer, list(_BLIND_PHRASES))
        evidence = {**evidence_base, "observed": observed, "admitted_blind": admitted_blind}

        if saw_color:
            # It named the color it could only know by looking — vision delivered.
            result.findings.append(
                Finding(
                    id="vision.color",
                    title="Vision delivered",
                    status=Status.PASS,
                    severity=Severity.INFO,
                    summary=(
                        "Model correctly identified the known image color "
                        f"({'/'.join(_EXPECTED_COLORS)}); claimed vision is delivered."
                    ),
                    evidence=evidence,
                )
            )
            result.status = Status.PASS
            result.score = 100.0
            return result

        # No expected color. Whether it refused, hallucinated, or named a wrong
        # color, the upshot is the same: the claimed vision is not delivered.
        if admitted_blind:
            summary = (
                "Model claims vision in its profile but reported it cannot see / "
                "process the image (possible text-only substitute)."
            )
        else:
            summary = (
                "Model claims vision but did not name the known image color "
                f"(expected {'/'.join(_EXPECTED_COLORS)}); answer is wrong or evasive, "
                "so the claimed vision is not delivered (possible text-only substitute)."
            )
        result.findings.append(
            Finding(
                id="vision.color",
                title="Claimed vision not delivered",
                status=Status.WARN,
                severity=Severity.MEDIUM,
                summary=summary,
                evidence=evidence,
                recommendation=(
                    "Confirm the served engine accepts and reasons over image input; "
                    "a text-only model behind a vision-claimed id is a 货不对板 mismatch."
                ),
            )
        )
        result.status = Status.WARN
        result.score = 0.0
        return result
