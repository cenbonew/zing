"""Image-generation and audio/TTS auditor tests.

These cover the non-chat media surface: ``zing.media_audit.audit_image`` /
``audit_audio`` plus the OpenAI-compatible client's ``images_generate`` /
``audio_speech`` methods and the pure-stdlib header parsers.

Sample bytes are synthesized in-test — a real solid-color PNG built with zlib
(mirroring ``zing/detectors/vision.py``) and a real WAV written by the stdlib
``wave`` module — so no image/audio library is needed. We inject an
``httpx.MockTransport`` into the real client (mirroring ``tests/test_embed.py``)
and patch ``make_client`` so the auditor uses our transport-backed client.

Scenarios:
  * /images/generations — correct-size PNG => PASS; wrong-size PNG => size_match
    FAIL; the SAME image for two prompts => distinctness WARN; non-image bytes =>
    format FAIL.
  * /audio/speech — valid WAV with duration => PASS; empty/garbage => FAIL;
    identical bytes for different inputs => distinctness WARN.
"""

from __future__ import annotations

import base64
import io
import json
import struct
import wave
import zlib

import httpx
import pytest

from zing.clients import OpenAICompatibleClient
from zing.media_audit import (
    audit_audio,
    audit_image,
    detect_audio,
    detect_image,
    wav_duration_seconds,
)
from zing.models import TargetConfig

BASE = "https://relay.media.test/v1"
IMG_MODEL = "dall-e-3"
TTS_MODEL = "tts-1"
SECRET = "sk-media-secret-key-123456"

OPENAI_SIZES = ["1024x1024", "1792x1024", "1024x1792"]


# --------------------------------------------------------------------------- #
# Sample byte synthesis (stdlib only)
# --------------------------------------------------------------------------- #
def make_png(width: int, height: int, rgb: tuple[int, int, int] = (255, 140, 0)) -> bytes:
    """A real width x height solid-color truecolor PNG, built with zlib only."""

    def _chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + body + struct.pack(">I", crc)

    r, g, b = rgb
    scanline = b"\x00" + bytes((r, g, b)) * width
    raw = scanline * height
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, 9)
    return signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def make_wav(seconds: float, freq: float = 440.0, rate: int = 8000) -> bytes:
    """A real mono 16-bit WAV of the given duration, written by the wave module."""
    import math

    n = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            val = int(32767 * 0.2 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Mock endpoints
# --------------------------------------------------------------------------- #
class ImageMock:
    """A minimal OpenAI-shaped /images/generations endpoint via MockTransport."""

    def __init__(
        self,
        *,
        width: int = 1024,
        height: int = 1024,
        served_model: str = IMG_MODEL,
        status: int = 200,
        # When True, every prompt returns the SAME image (the distinctness tell).
        fixed: bool = False,
        # When True, return non-image bytes (e.g. an HTML error page).
        garbage: bool = False,
    ):
        self.width = width
        self.height = height
        self.served_model = served_model
        self.status = status
        self.fixed = fixed
        self.garbage = garbage
        self.requests: list[dict] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append(body)
        if not request.url.path.endswith("/images/generations"):
            return httpx.Response(404, json={"error": {"message": "no route"}})
        if self.status >= 400:
            return httpx.Response(
                self.status,
                json={"error": {"message": f"bad key {SECRET}", "type": "invalid_request_error"}},
            )
        if self.garbage:
            payload = b"<html><body>error</body></html>"
        else:
            # A distinct color per prompt unless fixed (so distinctness varies).
            prompt = body.get("prompt", "")
            rgb = (255, 140, 0) if self.fixed else (abs(hash(prompt)) % 256, 140, 0)
            payload = make_png(self.width, self.height, rgb)
        b64 = base64.b64encode(payload).decode("ascii")
        return httpx.Response(
            200,
            json={"created": 1, "model": self.served_model, "data": [{"b64_json": b64}]},
        )


class AudioMock:
    """A minimal OpenAI-shaped /audio/speech endpoint via MockTransport."""

    def __init__(
        self,
        *,
        served_model: str = TTS_MODEL,
        status: int = 200,
        empty: bool = False,
        garbage: bool = False,
        fixed: bool = False,
    ):
        self.served_model = served_model
        self.status = status
        self.empty = empty
        self.garbage = garbage
        self.fixed = fixed
        self.requests: list[dict] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append(body)
        if not request.url.path.endswith("/audio/speech"):
            return httpx.Response(404, json={"error": {"message": "no route"}})
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"message": f"boom {SECRET}"}})
        if self.empty:
            return httpx.Response(200, content=b"")
        if self.garbage:
            return httpx.Response(200, content=b"<html>not audio</html>")
        text = body.get("input", "")
        # Duration scales with input length unless fixed (the placeholder tell).
        seconds = 0.5 if self.fixed else max(0.25, len(text) / 40.0)
        return httpx.Response(
            200,
            content=make_wav(seconds),
            headers={"content-type": "audio/wav", "x-model": self.served_model},
        )


def _img_target(**kw) -> TargetConfig:
    return TargetConfig(name="t", kind="target", base_url=BASE, api_key=SECRET, model=IMG_MODEL, **kw)


def _aud_target(**kw) -> TargetConfig:
    return TargetConfig(name="t", kind="target", base_url=BASE, api_key=SECRET, model=TTS_MODEL, **kw)


@pytest.fixture
def patch_client(monkeypatch):
    """Patch zing.media_audit.make_client to wire a chosen mock transport in."""

    def _install(mock) -> None:
        def _factory(target, *, transport=None):
            return OpenAICompatibleClient(target, transport=mock.transport)

        monkeypatch.setattr("zing.media_audit.make_client", _factory)

    return _install


def _finding(verdict: dict, finding_id: str) -> dict:
    for f in verdict["findings"]:
        if f["id"] == finding_id:
            return f
    raise AssertionError(
        f"finding {finding_id!r} not found in {[f['id'] for f in verdict['findings']]}"
    )


# --------------------------------------------------------------------------- #
# Pure parsers
# --------------------------------------------------------------------------- #
def test_detect_png_dimensions():
    fmt, w, h = detect_image(make_png(1792, 1024))
    assert fmt == "png"
    assert (w, h) == (1792, 1024)


def test_detect_image_rejects_non_image():
    assert detect_image(b"<html>nope</html>") == (None, None, None)


def test_detect_audio_and_wav_duration():
    wav = make_wav(1.0, rate=8000)
    assert detect_audio(wav) == "wav"
    dur = wav_duration_seconds(wav)
    assert dur == pytest.approx(1.0, abs=0.05)
    assert detect_audio(b"<html>") is None
    assert detect_audio(b"") is None


# --------------------------------------------------------------------------- #
# Client methods directly
# --------------------------------------------------------------------------- #
async def test_client_images_generate_decodes_b64():
    mock = ImageMock(width=512, height=512)
    async with OpenAICompatibleClient(_img_target(), transport=mock.transport) as c:
        outcome, images = await c.images_generate("a cat", "512x512", n=1)
    assert outcome.ok
    assert outcome.model_returned == IMG_MODEL
    assert len(images) == 1
    assert detect_image(images[0]) == ("png", 512, 512)


async def test_client_images_error_is_redacted():
    mock = ImageMock(status=401)
    async with OpenAICompatibleClient(_img_target(), transport=mock.transport) as c:
        outcome, images = await c.images_generate("a cat", "1024x1024")
    assert not outcome.ok and outcome.status_code == 401
    assert images == []
    assert SECRET not in (outcome.error_message or "")
    assert SECRET not in json.dumps(outcome.raw_error or {})


async def test_client_audio_speech_returns_bytes():
    mock = AudioMock()
    async with OpenAICompatibleClient(_aud_target(), transport=mock.transport) as c:
        outcome, audio = await c.audio_speech("hello world", "alloy", "wav")
    assert outcome.ok
    assert detect_audio(audio) == "wav"


async def test_client_audio_error_is_redacted():
    mock = AudioMock(status=403)
    async with OpenAICompatibleClient(_aud_target(), transport=mock.transport) as c:
        outcome, audio = await c.audio_speech("hi", "alloy", "wav")
    assert not outcome.ok and outcome.status_code == 403
    assert audio == b""
    assert SECRET not in (outcome.error_message or "")


# --------------------------------------------------------------------------- #
# Image audit
# --------------------------------------------------------------------------- #
async def test_image_correct_size_pass(patch_client):
    patch_client(ImageMock(width=1024, height=1024))
    verdict = await audit_image(_img_target(), size="1024x1024", claimed_sizes=OPENAI_SIZES)
    assert verdict["risk_level"] == "clean"
    assert verdict["score"] == 100.0
    sm = _finding(verdict, "image.size_match")
    assert sm["status"] == "pass"
    fmt = _finding(verdict, "image.format")
    assert fmt["evidence"]["format"] == "png"


async def test_image_wrong_size_is_high_severity_mismatch(patch_client):
    # Request 1792x1024 but the relay downscales to 1024x1024.
    patch_client(ImageMock(width=1024, height=1024))
    verdict = await audit_image(_img_target(), size="1792x1024", claimed_sizes=OPENAI_SIZES)
    assert verdict["risk_level"] == "high"
    sm = _finding(verdict, "image.size_match")
    assert sm["status"] == "fail"
    assert sm["severity"] == "high"
    assert sm["evidence"]["requested"] == "1792x1024"
    assert sm["evidence"]["detected"] == "1024x1024"


async def test_image_size_not_in_claimed_fails(patch_client):
    # Honors a 768x768 request, but that size is not a DALL-E-3 native size.
    patch_client(ImageMock(width=768, height=768))
    verdict = await audit_image(_img_target(), size="768x768", claimed_sizes=OPENAI_SIZES)
    sm = _finding(verdict, "image.size_match")
    assert sm["status"] == "fail"
    assert sm["severity"] == "high"
    assert verdict["risk_level"] == "high"


async def test_image_fixed_placeholder_warns_distinctness(patch_client):
    patch_client(ImageMock(width=1024, height=1024, fixed=True))
    verdict = await audit_image(_img_target(), size="1024x1024", claimed_sizes=OPENAI_SIZES)
    dist = _finding(verdict, "image.distinctness")
    assert dist["status"] == "warn"
    assert dist["severity"] == "high"
    assert verdict["risk_level"] == "high"


async def test_image_non_image_bytes_format_fail(patch_client):
    patch_client(ImageMock(garbage=True))
    verdict = await audit_image(_img_target(), size="1024x1024", claimed_sizes=OPENAI_SIZES)
    fmt = _finding(verdict, "image.format")
    assert fmt["status"] == "fail"
    assert fmt["severity"] == "high"
    assert verdict["risk_level"] == "high"


async def test_image_connectivity_error(patch_client):
    patch_client(ImageMock(status=500))
    verdict = await audit_image(_img_target(), size="1024x1024", claimed_sizes=OPENAI_SIZES)
    assert verdict["risk_level"] == "inconclusive"
    conn = _finding(verdict, "image.connectivity")
    assert conn["status"] == "error"


# --------------------------------------------------------------------------- #
# Audio audit
# --------------------------------------------------------------------------- #
async def test_audio_valid_wav_pass(patch_client):
    patch_client(AudioMock())
    verdict = await audit_audio(_aud_target(), voice="alloy", fmt="wav")
    assert verdict["risk_level"] == "clean"
    assert verdict["score"] == 100.0
    fmt = _finding(verdict, "audio.format")
    assert fmt["status"] == "pass"
    nt = _finding(verdict, "audio.nontrivial")
    assert nt["status"] == "pass"


async def test_audio_empty_is_fail(patch_client):
    patch_client(AudioMock(empty=True))
    verdict = await audit_audio(_aud_target(), voice="alloy", fmt="wav")
    assert verdict["risk_level"] == "high"
    conn = _finding(verdict, "audio.connectivity")
    assert conn["status"] == "fail"


async def test_audio_garbage_is_format_fail(patch_client):
    patch_client(AudioMock(garbage=True))
    verdict = await audit_audio(_aud_target(), voice="alloy", fmt="wav")
    fmt = _finding(verdict, "audio.format")
    assert fmt["status"] == "fail"
    assert fmt["severity"] == "high"
    assert verdict["risk_level"] == "high"


async def test_audio_fixed_placeholder_warns_distinctness(patch_client):
    patch_client(AudioMock(fixed=True))
    verdict = await audit_audio(_aud_target(), voice="alloy", fmt="wav")
    dist = _finding(verdict, "audio.distinctness")
    assert dist["status"] == "warn"
    assert dist["severity"] == "high"
    # The non-trivial check also warns (length did not scale) — both push HIGH.
    assert verdict["risk_level"] == "high"


async def test_audio_connectivity_error(patch_client):
    patch_client(AudioMock(status=500))
    verdict = await audit_audio(_aud_target(), voice="alloy", fmt="wav")
    assert verdict["risk_level"] == "inconclusive"
    conn = _finding(verdict, "audio.connectivity")
    assert conn["status"] == "error"
