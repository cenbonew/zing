"""Standalone auditor for image-generation and audio/TTS endpoints.

These are NON-chat surfaces. The 9-dimension chat detector pipeline (identity,
streaming, tool calls, ...) is meaningless for an endpoint that returns a PNG or a
WAV, so this module bypasses the runner and runs a small set of media-specific
checks directly against an OpenAI-shaped client.

The headline 货不对板 signal for images is a SIZE mismatch: a relay that accepts a
``1792x1024`` request but returns a downscaled ``1024x1024`` (or a model whose only
real size is ``512x512``) is serving a different/cheaper image model. For audio the
tells are a body that is not actually the requested audio format (HTML/JSON/empty
instead of WAV/MP3), a clip whose duration does not scale with the input, or
identical bytes for different inputs (a fixed placeholder).

All parsing is pure stdlib — no Pillow, no numpy. Image dimensions are read from
header bytes (PNG IHDR, JPEG SOF markers, GIF, WebP); audio is identified by magic
bytes and WAV duration is decoded with the stdlib :mod:`wave` module.
"""

from __future__ import annotations

import hashlib
import io
import struct
import wave
from typing import Any

from zing.clients import make_client
from zing.embed_audit import _finding, _verdict
from zing.models import Severity, Status, TargetConfig

# Two clearly-different prompts for the image distinctness probe.
_IMG_PROMPT_A = "A photorealistic red apple on a white table, studio lighting."
_IMG_PROMPT_B = "A blue sailboat on a calm ocean at sunset, watercolor style."

# Two clearly-different inputs for the audio probes. ``B`` is markedly LONGER than
# ``A`` so a genuine TTS engine must produce a longer clip.
_AUD_SHORT = "Hello."
_AUD_LONG = (
    "Hello there. This is a substantially longer sentence used to verify that a "
    "genuine text-to-speech engine produces audio whose length grows with the "
    "input text rather than returning a fixed-size placeholder clip."
)


# --------------------------------------------------------------------------- #
# Image header parsing (pure stdlib — PNG / JPEG / GIF / WebP)
# --------------------------------------------------------------------------- #
def detect_image(data: bytes) -> tuple[str | None, int | None, int | None]:
    """Identify an image and read its pixel dimensions from header bytes.

    Returns ``(format, width, height)``. ``format`` is one of
    ``png``/``jpeg``/``gif``/``webp`` (or ``None`` if the magic bytes do not match
    a supported image format). ``width``/``height`` are ``None`` when the header is
    truncated or unparseable. No image library is used.
    """
    if len(data) < 12:
        return None, None, None

    # PNG: 8-byte signature, then an IHDR chunk whose data starts with W,H (BE u32).
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            width, height = struct.unpack(">II", data[16:24])
            return "png", width, height
        return "png", None, None

    # GIF: "GIF87a"/"GIF89a", then logical-screen W,H as little-endian u16.
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return "gif", width, height

    # WebP: RIFF....WEBP, then a VP8 / VP8L / VP8X chunk carrying the dimensions.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _detect_webp(data)

    # JPEG: SOI (FF D8), then walk markers to the first SOFn frame header.
    if data[:2] == b"\xff\xd8":
        return _detect_jpeg(data)

    return None, None, None


def _detect_jpeg(data: bytes) -> tuple[str, int | None, int | None]:
    """Walk JPEG segments to the first Start-Of-Frame and read its W,H."""
    i = 2
    n = len(data)
    # SOF markers carrying frame dimensions (baseline, progressive, ...). Exclude
    # the non-frame markers in the 0xC4/0xC8/0xCC slots.
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    while i + 4 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in sof_markers:
            if i + 9 <= n:
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpeg", width, height
            return "jpeg", None, None
        i += 2 + seg_len
    return "jpeg", None, None


def _detect_webp(data: bytes) -> tuple[str, int | None, int | None]:
    """Read the dimensions from a WebP VP8 / VP8L / VP8X chunk."""
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        # Lossy: 3-byte frame tag, 0x9d012a sync, then 14-bit width/height (LE).
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return "webp", width, height
    if chunk == b"VP8L" and len(data) >= 25:
        # Lossless: 0x2f signature, then 14-bit-1 width/height packed across 4 bytes.
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return "webp", width, height
    if chunk == b"VP8X" and len(data) >= 30:
        # Extended: 24-bit-1 canvas width/height (LE) at offsets 24 and 27.
        width = 1 + (data[24] | (data[25] << 8) | (data[26] << 16))
        height = 1 + (data[27] | (data[28] << 8) | (data[29] << 16))
        return "webp", width, height
    return "webp", None, None


def _parse_size(size: str) -> tuple[int, int] | None:
    """Parse a ``"WxH"`` size string into ``(width, height)`` ints, else None."""
    parts = size.lower().replace("×", "x").split("x")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Audio magic-byte parsing (pure stdlib — WAV via the wave module)
# --------------------------------------------------------------------------- #
def detect_audio(data: bytes) -> str | None:
    """Identify an audio container from magic bytes.

    Returns ``wav``/``mp3``/``ogg``/``flac`` or ``None`` (HTML/JSON/empty/garbage).
    """
    if len(data) < 4:
        return None
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    # MP3: an ID3v2 tag, or a raw MPEG-audio frame sync (0xFF, then 0xEx/0xFx).
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None


def wav_duration_seconds(data: bytes) -> float | None:
    """Decode a WAV clip's duration in seconds using the stdlib wave module."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate <= 0:
                return None
            return frames / float(rate)
    except (wave.Error, EOFError, struct.error, ValueError):
        return None


def _format_matches(detected: str | None, requested: str) -> bool:
    """Whether the detected container is consistent with the requested format."""
    if detected is None:
        return False
    req = requested.lower().strip()
    # opus is carried in an Ogg container; pcm has no container of its own.
    if req in ("opus", "ogg"):
        return detected == "ogg"
    return detected == req


# --------------------------------------------------------------------------- #
# Image audit
# --------------------------------------------------------------------------- #
async def audit_image(
    target: TargetConfig, *, size: str, claimed_sizes: list[str]
) -> dict[str, Any]:
    """Run image-generation checks against ``target``.

    ``size`` is the requested ``"WxH"`` size; ``claimed_sizes`` is the KB list of
    sizes the claimed model natively supports (empty = unknown). Returns the same
    small verdict dict as the embedding auditor: ``risk_level``, ``score``,
    ``findings``, ``target``.
    """
    findings: list[dict[str, Any]] = []

    async with make_client(target) as client:
        outcome_a, images_a = await client.images_generate(_IMG_PROMPT_A, size, n=1)
        outcome_b, images_b = await client.images_generate(_IMG_PROMPT_B, size, n=1)

    # 1) Connectivity ------------------------------------------------------- #
    if not outcome_a.ok:
        findings.append(
            _finding(
                "image.connectivity",
                "Image endpoint unreachable",
                Status.ERROR,
                Severity.HIGH,
                f"POST /images/generations failed: "
                f"{outcome_a.error_message or outcome_a.status_code}",
                {"status_code": outcome_a.status_code, "error": outcome_a.error_message},
            )
        )
        return _verdict(findings, target)

    if not images_a:
        findings.append(
            _finding(
                "image.connectivity",
                "Image response carried no image",
                Status.ERROR,
                Severity.HIGH,
                "Endpoint returned 200 but no decodable image bytes.",
                {"images_returned": 0},
            )
        )
        return _verdict(findings, target)

    img = images_a[0]
    findings.append(
        _finding(
            "image.connectivity",
            "Image endpoint reachable",
            Status.PASS,
            Severity.INFO,
            f"Generation returned {len(images_a)} image(s), {len(img)} bytes.",
            {"images": len(images_a), "bytes": len(img)},
        )
    )

    # 2) Format (valid magic + decodable header) ---------------------------- #
    fmt, width, height = detect_image(img)
    if fmt is None:
        findings.append(
            _finding(
                "image.format",
                "Returned bytes are not a recognizable image",
                Status.FAIL,
                Severity.HIGH,
                "The response body did not start with a known image magic "
                "(PNG/JPEG/GIF/WebP) — not a real image generation.",
                {"head_hex": img[:16].hex(), "bytes": len(img)},
            )
        )
        return _verdict(findings, target)

    if width is None or height is None:
        findings.append(
            _finding(
                "image.format",
                "Image header truncated / undecodable",
                Status.FAIL,
                Severity.HIGH,
                f"Detected a {fmt} image but could not read its dimensions from the "
                f"header — the file is truncated or malformed.",
                {"format": fmt, "bytes": len(img)},
            )
        )
        return _verdict(findings, target)

    detected_size = f"{width}x{height}"
    findings.append(
        _finding(
            "image.format",
            "Returned a decodable image",
            Status.PASS,
            Severity.INFO,
            f"Detected a {fmt} image, {detected_size}.",
            {"format": fmt, "width": width, "height": height},
        )
    )

    # 3) Size match (the headline mismatch signal) -------------------------- #
    requested = _parse_size(size)
    in_claimed = (detected_size in claimed_sizes) if claimed_sizes else None
    if requested is None:
        findings.append(
            _finding(
                "image.size_match",
                "Requested size unparseable — recording observed size",
                Status.INFO,
                Severity.INFO,
                f"Could not parse requested size {size!r}; observed {detected_size}.",
                {"requested": size, "detected": detected_size},
            )
        )
    elif (width, height) != requested:
        findings.append(
            _finding(
                "image.size_match",
                "Returned image size does NOT match the request",
                Status.FAIL,
                Severity.HIGH,
                f"Requested {size} but the relay returned {detected_size}. A "
                f"downscaled / wrong-size image is strong evidence of a substituted "
                f"or cheaper image model (货不对板).",
                {
                    "requested": size,
                    "detected": detected_size,
                    "claimed_sizes": claimed_sizes,
                    "in_claimed_sizes": in_claimed,
                },
            )
        )
    elif claimed_sizes and detected_size not in claimed_sizes:
        findings.append(
            _finding(
                "image.size_match",
                "Returned size not among the claimed model's native sizes",
                Status.FAIL,
                Severity.HIGH,
                f"The relay honored the {size} request, but {detected_size} is not "
                f"among the claimed model's native sizes {claimed_sizes} — the "
                f"served model is not the one claimed.",
                {"detected": detected_size, "claimed_sizes": claimed_sizes},
            )
        )
    else:
        findings.append(
            _finding(
                "image.size_match",
                "Returned image size matches the request",
                Status.PASS,
                Severity.INFO,
                f"Returned {detected_size}, exactly the requested size"
                + (" and a claimed native size." if claimed_sizes else "."),
                {
                    "requested": size,
                    "detected": detected_size,
                    "claimed_sizes": claimed_sizes,
                    "in_claimed_sizes": in_claimed,
                },
            )
        )

    # 4) Count (n=1 returns exactly 1) -------------------------------------- #
    if len(images_a) == 1:
        findings.append(
            _finding(
                "image.count",
                "Honors the requested image count",
                Status.PASS,
                Severity.INFO,
                "An n=1 request returned exactly one image.",
                {"requested_n": 1, "returned": len(images_a)},
            )
        )
    else:
        findings.append(
            _finding(
                "image.count",
                "Returned an unexpected image count",
                Status.WARN,
                Severity.MEDIUM,
                f"An n=1 request returned {len(images_a)} images.",
                {"requested_n": 1, "returned": len(images_a)},
            )
        )

    # 5) Distinctness (two different prompts -> different bytes) ------------- #
    hash_a = hashlib.sha256(img).hexdigest()
    if outcome_b.ok and images_b:
        hash_b = hashlib.sha256(images_b[0]).hexdigest()
        if hash_a == hash_b:
            findings.append(
                _finding(
                    "image.distinctness",
                    "Two different prompts produced identical images",
                    Status.WARN,
                    Severity.HIGH,
                    "Unrelated prompts returned byte-identical images — the endpoint "
                    "is serving a fixed placeholder rather than generating.",
                    {"hash_a": hash_a[:16], "hash_b": hash_b[:16]},
                )
            )
        else:
            findings.append(
                _finding(
                    "image.distinctness",
                    "Different prompts produce different images",
                    Status.PASS,
                    Severity.INFO,
                    "Two unrelated prompts returned distinct images.",
                    {"hash_a": hash_a[:16], "hash_b": hash_b[:16]},
                )
            )
    else:
        findings.append(
            _finding(
                "image.distinctness",
                "Distinctness probe inconclusive",
                Status.INFO,
                Severity.INFO,
                "The second generation did not return a usable image to compare.",
                {"second_ok": outcome_b.ok, "second_images": len(images_b)},
            )
        )

    # 6) Echoed model field ------------------------------------------------- #
    findings.append(
        _finding(
            "image.model_field",
            "Echoed model field",
            Status.INFO,
            Severity.INFO,
            f"Relay reported model: {outcome_a.model_returned or '(none)'}.",
            {
                "model_returned": outcome_a.model_returned,
                "model_requested": target.model,
                "claimed_model": target.claimed_model,
            },
        )
    )

    return _verdict(findings, target)


# --------------------------------------------------------------------------- #
# Audio / TTS audit
# --------------------------------------------------------------------------- #
async def audit_audio(
    target: TargetConfig, *, voice: str, fmt: str
) -> dict[str, Any]:
    """Run audio/TTS checks against ``target``.

    ``voice`` is the requested voice id; ``fmt`` is the requested response format
    (``wav``/``mp3``/``opus``/``flac``). Returns the same verdict dict shape as the
    embedding auditor.
    """
    findings: list[dict[str, Any]] = []

    async with make_client(target) as client:
        outcome_s, audio_s = await client.audio_speech(_AUD_SHORT, voice, fmt)
        outcome_l, audio_l = await client.audio_speech(_AUD_LONG, voice, fmt)

    # 1) Connectivity ------------------------------------------------------- #
    if not outcome_s.ok:
        findings.append(
            _finding(
                "audio.connectivity",
                "Audio endpoint unreachable",
                Status.ERROR,
                Severity.HIGH,
                f"POST /audio/speech failed: "
                f"{outcome_s.error_message or outcome_s.status_code}",
                {"status_code": outcome_s.status_code, "error": outcome_s.error_message},
            )
        )
        return _verdict(findings, target)

    if not audio_s:
        findings.append(
            _finding(
                "audio.connectivity",
                "Audio response was empty",
                Status.FAIL,
                Severity.HIGH,
                "Endpoint returned 200 but an empty body — no audio was produced.",
                {"bytes": 0},
            )
        )
        return _verdict(findings, target)

    findings.append(
        _finding(
            "audio.connectivity",
            "Audio endpoint reachable",
            Status.PASS,
            Severity.INFO,
            f"Speech synthesis returned {len(audio_s)} bytes.",
            {"bytes": len(audio_s)},
        )
    )

    # 2) Format (magic bytes match the requested fmt) ----------------------- #
    detected = detect_audio(audio_s)
    if detected is None:
        findings.append(
            _finding(
                "audio.format",
                "Returned bytes are not recognizable audio",
                Status.FAIL,
                Severity.HIGH,
                "The body did not start with a known audio magic "
                "(WAV/MP3/OGG/FLAC) — likely HTML/JSON/garbage, not real audio.",
                {"head_hex": audio_s[:16].hex(), "bytes": len(audio_s)},
            )
        )
        return _verdict(findings, target)

    findings.append(
        _finding(
            "audio.format",
            "Returned recognizable audio",
            Status.PASS,
            Severity.INFO,
            f"Detected a {detected} audio container.",
            {"detected": detected, "requested": fmt},
        )
    )

    # 3) Format honored (detected == requested) ----------------------------- #
    if _format_matches(detected, fmt):
        findings.append(
            _finding(
                "audio.format_honored",
                "Endpoint honored the requested format",
                Status.PASS,
                Severity.INFO,
                f"Requested {fmt}; returned {detected}.",
                {"requested": fmt, "detected": detected},
            )
        )
    else:
        findings.append(
            _finding(
                "audio.format_honored",
                "Endpoint ignored the requested format",
                Status.WARN,
                Severity.MEDIUM,
                f"Requested {fmt} but the body is {detected}. A relay that ignores "
                f"response_format may be passing through a fixed upstream format.",
                {"requested": fmt, "detected": detected},
            )
        )

    # 4) Non-trivial: length must scale with the input ---------------------- #
    nontrivial_ev: dict[str, Any] = {
        "short_bytes": len(audio_s),
        "long_bytes": len(audio_l),
        "second_ok": outcome_l.ok,
    }
    if detected == "wav":
        dur_s = wav_duration_seconds(audio_s)
        dur_l = wav_duration_seconds(audio_l) if (outcome_l.ok and audio_l) else None
        nontrivial_ev.update({"short_duration_s": dur_s, "long_duration_s": dur_l})
        if dur_s is None or dur_s <= 0:
            findings.append(
                _finding(
                    "audio.nontrivial",
                    "WAV clip has no decodable duration",
                    Status.FAIL,
                    Severity.HIGH,
                    "The WAV header decoded to zero / no frames — an empty or "
                    "truncated placeholder rather than synthesized speech.",
                    nontrivial_ev,
                )
            )
        elif dur_l is not None and dur_l > dur_s:
            findings.append(
                _finding(
                    "audio.nontrivial",
                    "Audio length scales with input",
                    Status.PASS,
                    Severity.INFO,
                    f"A longer input produced a longer clip ({dur_s:.2f}s -> "
                    f"{dur_l:.2f}s).",
                    nontrivial_ev,
                )
            )
        elif dur_l is not None:
            findings.append(
                _finding(
                    "audio.nontrivial",
                    "Audio length does not scale with input",
                    Status.WARN,
                    Severity.HIGH,
                    f"A much longer input did not yield a longer clip "
                    f"({dur_s:.2f}s -> {dur_l:.2f}s) — suggests a fixed placeholder.",
                    nontrivial_ev,
                )
            )
        else:
            findings.append(
                _finding(
                    "audio.nontrivial",
                    "Audio clip has a positive duration",
                    Status.PASS,
                    Severity.INFO,
                    f"Decoded a {dur_s:.2f}s clip; could not compare a second input.",
                    nontrivial_ev,
                )
            )
    else:
        # Non-WAV: assert byte length scales with input length.
        if outcome_l.ok and len(audio_l) > len(audio_s):
            findings.append(
                _finding(
                    "audio.nontrivial",
                    "Audio length scales with input",
                    Status.PASS,
                    Severity.INFO,
                    f"A longer input produced more audio bytes "
                    f"({len(audio_s)} -> {len(audio_l)}).",
                    nontrivial_ev,
                )
            )
        elif outcome_l.ok:
            findings.append(
                _finding(
                    "audio.nontrivial",
                    "Audio length does not scale with input",
                    Status.WARN,
                    Severity.HIGH,
                    f"A much longer input did not yield more audio bytes "
                    f"({len(audio_s)} -> {len(audio_l)}) — suggests a fixed "
                    f"placeholder.",
                    nontrivial_ev,
                )
            )
        else:
            findings.append(
                _finding(
                    "audio.nontrivial",
                    "Audio clip is non-empty",
                    Status.PASS,
                    Severity.INFO,
                    f"Returned {len(audio_s)} bytes; could not compare a second input.",
                    nontrivial_ev,
                )
            )

    # 5) Distinctness (different input -> different bytes) ------------------- #
    if outcome_l.ok and audio_l:
        hash_s = hashlib.sha256(audio_s).hexdigest()
        hash_l = hashlib.sha256(audio_l).hexdigest()
        if hash_s == hash_l:
            findings.append(
                _finding(
                    "audio.distinctness",
                    "Different inputs produced identical audio",
                    Status.WARN,
                    Severity.HIGH,
                    "Two different inputs returned byte-identical audio — a fixed "
                    "placeholder, not real synthesis.",
                    {"hash_short": hash_s[:16], "hash_long": hash_l[:16]},
                )
            )
        else:
            findings.append(
                _finding(
                    "audio.distinctness",
                    "Different inputs produce different audio",
                    Status.PASS,
                    Severity.INFO,
                    "Two different inputs returned distinct audio.",
                    {"hash_short": hash_s[:16], "hash_long": hash_l[:16]},
                )
            )
    else:
        findings.append(
            _finding(
                "audio.distinctness",
                "Distinctness probe inconclusive",
                Status.INFO,
                Severity.INFO,
                "The second synthesis did not return usable audio to compare.",
                {"second_ok": outcome_l.ok, "second_bytes": len(audio_l)},
            )
        )

    # 6) Echoed model field ------------------------------------------------- #
    findings.append(
        _finding(
            "audio.model_field",
            "Echoed model field",
            Status.INFO,
            Severity.INFO,
            f"Relay reported model: {outcome_s.model_returned or '(none)'}.",
            {
                "model_returned": outcome_s.model_returned,
                "model_requested": target.model,
                "claimed_model": target.claimed_model,
            },
        )
    )

    return _verdict(findings, target)
