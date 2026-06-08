"""Embedding and rerank auditor tests.

These cover the non-chat surface: ``zing.embed_audit.audit_embeddings`` and
``audit_rerank`` plus the OpenAI-compatible client's ``embeddings``/``rerank``
methods. We inject an ``httpx.MockTransport`` into the real client (mirroring the
mock-client style in ``tests/test_anthropic.py``) so no network is touched, and
patch ``make_client`` so the auditor uses our transport-backed client.

Scenarios:
  * /embeddings — correct dims => PASS; wrong dims => high-severity mismatch;
    identical vectors for different inputs => a distinctness warning.
  * /rerank — correct top => PASS; wrong top => WARN.
"""

from __future__ import annotations

import hashlib
import json
import math

import httpx
import pytest

from zing.clients import OpenAICompatibleClient
from zing.embed_audit import audit_embeddings, audit_rerank, cosine_similarity
from zing.models import TargetConfig

BASE = "https://relay.embed.test/v1"
MODEL = "text-embedding-3-large"
SECRET = "sk-embed-secret-key-123456"


def _stable_key(text: str) -> int:
    """A process-stable integer key for ``text`` (NOT the salted builtin ``hash``)."""
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)


def _unit_vector(dim: int, key: int) -> list[float]:
    """Deterministic vector of length ``dim``.

    A dominant spike at a key-determined index makes *distinct* keys near-orthogonal
    (low cosine, so distinctness PASSES), while an *identical* key yields an identical
    vector (determinism is exact). Independent of ``PYTHONHASHSEED`` — same key in,
    same vector out, every process — so the distinctness check can never flake.
    """
    v = [0.001 * math.sin(key % 997 + i) for i in range(dim)]
    v[key % dim] += 10.0
    return v


class EmbedMock:
    """A minimal OpenAI-shaped /embeddings + /rerank endpoint via MockTransport."""

    def __init__(
        self,
        *,
        dim: int = 3072,
        served_model: str = MODEL,
        status: int = 200,
        # When True, every input returns the SAME vector (the distinctness tell).
        collapse: bool = False,
        # Rerank: which document index the relay ranks first.
        rerank_top: int = 2,
    ):
        self.dim = dim
        self.served_model = served_model
        self.status = status
        self.collapse = collapse
        self.rerank_top = rerank_top
        self.requests: list[dict] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        self.requests.append(body)
        if request.url.path.endswith("/embeddings"):
            return self._embeddings(body)
        if request.url.path.endswith("/rerank"):
            return self._rerank(body)
        return httpx.Response(404, json={"error": {"message": "no route"}})

    def _embeddings(self, body: dict) -> httpx.Response:
        if self.status >= 400:
            return httpx.Response(
                self.status,
                json={"error": {"message": f"bad key {SECRET}", "type": "invalid_request_error"}},
            )
        inputs = body.get("input") or []
        data = []
        for i, text in enumerate(inputs):
            # Identical vector for every input when collapsed; otherwise a vector
            # that is identical for identical text (determinism) but distinct for
            # distinct text (distinctness).
            key = 0 if self.collapse else _stable_key(text)
            data.append({"object": "embedding", "index": i, "embedding": _unit_vector(self.dim, key)})
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": self.served_model,
                "data": data,
                "usage": {"prompt_tokens": 10, "total_tokens": 10},
            },
        )

    def _rerank(self, body: dict) -> httpx.Response:
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"message": "boom"}})
        docs = body.get("documents") or []
        results = []
        for i in range(len(docs)):
            score = 0.99 if i == self.rerank_top else 0.10 + i * 0.01
            results.append({"index": i, "relevance_score": score})
        # Return them deliberately UNSORTED to exercise the client's sort.
        return httpx.Response(200, json={"model": self.served_model, "results": results})


def _target(**kw) -> TargetConfig:
    return TargetConfig(name="t", kind="target", base_url=BASE, api_key=SECRET, model=MODEL, **kw)


@pytest.fixture
def patch_client(monkeypatch):
    """Patch zing.embed_audit.make_client to wire a chosen mock transport in."""

    def _install(mock: EmbedMock) -> None:
        def _factory(target, *, transport=None):
            return OpenAICompatibleClient(target, transport=mock.transport)

        monkeypatch.setattr("zing.embed_audit.make_client", _factory)

    return _install


# --------------------------------------------------------------------------- #
# Pure math
# --------------------------------------------------------------------------- #
def test_cosine_similarity_basics():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([], [1.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


# --------------------------------------------------------------------------- #
# Client methods directly
# --------------------------------------------------------------------------- #
async def test_client_embeddings_shape():
    mock = EmbedMock(dim=8)
    async with OpenAICompatibleClient(_target(), transport=mock.transport) as c:
        outcome, vectors = await c.embeddings(["a", "b"])
    assert outcome.ok
    assert outcome.model_returned == MODEL
    assert len(vectors) == 2
    assert all(len(v) == 8 for v in vectors)


async def test_client_embeddings_error_is_redacted():
    mock = EmbedMock(status=401)
    async with OpenAICompatibleClient(_target(), transport=mock.transport) as c:
        outcome, vectors = await c.embeddings(["a"])
    assert not outcome.ok and outcome.status_code == 401
    assert vectors == []
    assert SECRET not in (outcome.error_message or "")
    assert SECRET not in json.dumps(outcome.raw_error or {})


async def test_client_rerank_is_sorted():
    mock = EmbedMock(rerank_top=2)
    async with OpenAICompatibleClient(_target(), transport=mock.transport) as c:
        outcome, results = await c.rerank("q", ["a", "b", "c", "d"])
    assert outcome.ok
    assert results[0]["index"] == 2
    scores = [r["relevance_score"] for r in results]
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Embedding audit
# --------------------------------------------------------------------------- #
async def test_embed_correct_dims_pass(patch_client):
    patch_client(EmbedMock(dim=3072))
    verdict = await audit_embeddings(_target(), claimed_dimensions=3072)
    assert verdict["risk_level"] == "clean"
    assert verdict["score"] == 100.0
    dim = _finding(verdict, "embed.dimension")
    assert dim["status"] == "pass"


async def test_embed_wrong_dims_is_high_severity_mismatch(patch_client):
    # Claim 3072-d (text-embedding-3-large) but the relay returns 1536-d.
    patch_client(EmbedMock(dim=1536))
    verdict = await audit_embeddings(_target(), claimed_dimensions=3072)
    assert verdict["risk_level"] == "high"
    dim = _finding(verdict, "embed.dimension")
    assert dim["status"] == "fail"
    assert dim["severity"] == "high"
    assert dim["evidence"] == {"returned": 1536, "claimed": 3072}


async def test_embed_collapsed_vectors_warn_on_distinctness(patch_client):
    # Same vector for every input: determinism trivially passes but distinctness
    # must warn (and at high severity).
    patch_client(EmbedMock(dim=3072, collapse=True))
    verdict = await audit_embeddings(_target(), claimed_dimensions=3072)
    distinct = _finding(verdict, "embed.distinctness")
    assert distinct["status"] == "warn"
    assert distinct["severity"] == "high"
    assert verdict["risk_level"] == "high"


async def test_embed_unknown_dims_skips_match(patch_client):
    patch_client(EmbedMock(dim=1024))
    verdict = await audit_embeddings(_target(), claimed_dimensions=0)
    dim = _finding(verdict, "embed.dimension")
    assert dim["status"] == "info"
    assert dim["evidence"]["returned"] == 1024


async def test_embed_connectivity_error(patch_client):
    patch_client(EmbedMock(status=500))
    verdict = await audit_embeddings(_target(), claimed_dimensions=3072)
    assert verdict["risk_level"] == "inconclusive"
    conn = _finding(verdict, "embed.connectivity")
    assert conn["status"] == "error"


# --------------------------------------------------------------------------- #
# Rerank audit
# --------------------------------------------------------------------------- #
_RR_QUERY = "What is the capital of France?"
_RR_DOCS = [
    "Bananas are a good source of potassium.",
    "The Great Wall of China is very long.",
    "Paris is the capital of France.",
    "Photosynthesis happens in plants.",
]


async def test_rerank_correct_top_pass(patch_client):
    patch_client(EmbedMock(rerank_top=2))
    verdict = await audit_rerank(_target(), _RR_QUERY, _RR_DOCS, expected_top_index=2)
    assert verdict["risk_level"] == "clean"
    ka = _finding(verdict, "rerank.known_answer")
    assert ka["status"] == "pass"


async def test_rerank_wrong_top_warn(patch_client):
    patch_client(EmbedMock(rerank_top=0))
    verdict = await audit_rerank(_target(), _RR_QUERY, _RR_DOCS, expected_top_index=2)
    ka = _finding(verdict, "rerank.known_answer")
    assert ka["status"] == "warn"
    assert ka["severity"] == "high"
    assert verdict["risk_level"] == "high"
    assert ka["evidence"]["top_index"] == 0


def _finding(verdict: dict, finding_id: str) -> dict:
    for f in verdict["findings"]:
        if f["id"] == finding_id:
            return f
    raise AssertionError(f"finding {finding_id!r} not found in {[f['id'] for f in verdict['findings']]}")
