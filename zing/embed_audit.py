"""Standalone auditor for embedding and rerank endpoints.

These are a NON-chat surface. The 9-dimension chat detector pipeline (identity,
streaming, tool calls, ...) is meaningless for a model that returns a vector, so
this module deliberately bypasses the runner and runs a small set of
embedding-specific checks directly against an OpenAI-shaped client.

The headline 货不对板 signal here is a DIMENSION mismatch: a relay that claims
``text-embedding-3-large`` (3072-d) but returns 1536-d vectors is serving a
different model. We also probe DETERMINISM (same input -> near-identical vector,
cosine ~ 1) and DISTINCTNESS (different inputs -> cosine well below 1), and echo
back the model field the relay reports.

All math is pure stdlib — no numpy dependency.
"""

from __future__ import annotations

import math
from typing import Any

from zing.clients import make_client
from zing.models import RiskLevel, Severity, Status, TargetConfig
from zing.utils.redact import fingerprint_secret

# A pair of clearly-unrelated inputs for the distinctness probe, plus a repeated
# input for the determinism probe.
_DET_INPUT = "The quick brown fox jumps over the lazy dog."
_DISTINCT_A = "A recipe for chocolate chip cookies with butter and brown sugar."
_DISTINCT_B = "Quarterly revenue grew on strong cloud-infrastructure demand."

# Cosine thresholds. Determinism should be essentially 1.0; distinct texts should
# be comfortably below 1.0 (identical vectors for different inputs is the tell).
_DET_MIN_COSINE = 0.9999
_DISTINCT_MAX_COSINE = 0.98


# --------------------------------------------------------------------------- #
# Pure math helpers (stdlib only)
# --------------------------------------------------------------------------- #
def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors. 0.0 if either is degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _finding(
    id_: str,
    title: str,
    status: Status,
    severity: Severity,
    summary: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": id_,
        "title": title,
        "status": status.value,
        "severity": severity.value,
        "summary": summary,
        "evidence": evidence or {},
    }


def _redacted_target(target: TargetConfig) -> dict[str, Any]:
    return {
        "name": target.name,
        "base_url": target.base_url,
        "model": target.model,
        "claimed_model": target.claimed_model,
        "declared_provider": target.declared_provider,
        "api_key_fingerprint": fingerprint_secret(target.api_key),
    }


def _verdict(
    findings: list[dict[str, Any]], target: TargetConfig
) -> dict[str, Any]:
    """Roll findings up into a risk level + 0-100 score."""
    has_fail = any(f["status"] == Status.FAIL.value for f in findings)
    has_error = any(f["status"] == Status.ERROR.value for f in findings)
    has_warn = any(f["status"] == Status.WARN.value for f in findings)
    # A high-severity *finding* (fail/warn) is a mismatch signal. An ERROR finding
    # (couldn't reach/parse the endpoint) is high-severity too, but it is evidence
    # of nothing about the model — that is handled as INCONCLUSIVE first.
    has_high = any(
        f["severity"] in (Severity.HIGH.value, Severity.CRITICAL.value)
        and f["status"] != Status.ERROR.value
        for f in findings
    )

    if has_error:
        # Could not reach / parse the endpoint — not a mismatch, but not clean.
        risk = RiskLevel.INCONCLUSIVE
        score = 50.0
    elif has_fail or has_high:
        risk = RiskLevel.HIGH
        score = 20.0
    elif has_warn:
        risk = RiskLevel.MEDIUM
        score = 65.0
    else:
        risk = RiskLevel.CLEAN
        score = 100.0

    return {
        "risk_level": risk.value,
        "score": score,
        "findings": findings,
        "target": _redacted_target(target),
    }


# --------------------------------------------------------------------------- #
# Embedding audit
# --------------------------------------------------------------------------- #
async def audit_embeddings(
    target: TargetConfig, claimed_dimensions: int
) -> dict[str, Any]:
    """Run embedding-specific checks against ``target``.

    ``claimed_dimensions`` is the native output dimensionality of the claimed
    model (0 = unknown, in which case the dimension check is skipped). Returns a
    small verdict dict: ``risk_level``, ``score``, ``findings``, ``target``.
    """
    findings: list[dict[str, Any]] = []

    async with make_client(target) as client:
        # One batched call covers connectivity + determinism + distinctness:
        # [det, det(repeat), distinct_a, distinct_b].
        outcome, vectors = await client.embeddings(
            [_DET_INPUT, _DET_INPUT, _DISTINCT_A, _DISTINCT_B]
        )

    # 1) Connectivity ------------------------------------------------------- #
    if not outcome.ok:
        findings.append(
            _finding(
                "embed.connectivity",
                "Embedding endpoint unreachable",
                Status.ERROR,
                Severity.HIGH,
                f"POST /embeddings failed: {outcome.error_message or outcome.status_code}",
                {"status_code": outcome.status_code, "error": outcome.error_message},
            )
        )
        return _verdict(findings, target)

    if len(vectors) < 4 or any(not v for v in vectors):
        findings.append(
            _finding(
                "embed.connectivity",
                "Embedding response malformed",
                Status.ERROR,
                Severity.HIGH,
                f"Expected 4 non-empty vectors, got {len(vectors)}.",
                {"returned_vectors": len(vectors)},
            )
        )
        return _verdict(findings, target)

    findings.append(
        _finding(
            "embed.connectivity",
            "Embedding endpoint reachable",
            Status.PASS,
            Severity.INFO,
            f"Got {len(vectors)} vectors of dimension {len(vectors[0])}.",
            {"vectors": len(vectors), "dimension": len(vectors[0])},
        )
    )

    det_a, det_b, dist_a, dist_b = vectors[0], vectors[1], vectors[2], vectors[3]
    returned_dim = len(det_a)

    # 2) Dimension match (the headline mismatch signal) --------------------- #
    if claimed_dimensions > 0:
        if returned_dim == claimed_dimensions:
            findings.append(
                _finding(
                    "embed.dimension",
                    "Vector dimension matches the claimed model",
                    Status.PASS,
                    Severity.INFO,
                    f"Returned {returned_dim}-d vectors, as expected for the claim.",
                    {"returned": returned_dim, "claimed": claimed_dimensions},
                )
            )
        else:
            findings.append(
                _finding(
                    "embed.dimension",
                    "Vector dimension does NOT match the claimed model",
                    Status.FAIL,
                    Severity.HIGH,
                    (
                        f"Returned {returned_dim}-d vectors but the claimed model "
                        f"produces {claimed_dimensions}-d. Strong evidence of a "
                        f"substituted embedding model (货不对板)."
                    ),
                    {"returned": returned_dim, "claimed": claimed_dimensions},
                )
            )
    else:
        findings.append(
            _finding(
                "embed.dimension",
                "Claimed dimension unknown — recording observed dimension",
                Status.INFO,
                Severity.INFO,
                f"No KB dimension for the claimed model; observed {returned_dim}-d.",
                {"returned": returned_dim, "claimed": 0},
            )
        )

    # 3) Determinism (same input twice -> cosine ~ 1) ----------------------- #
    det_cos = cosine_similarity(det_a, det_b)
    if det_cos >= _DET_MIN_COSINE:
        findings.append(
            _finding(
                "embed.determinism",
                "Embeddings are deterministic",
                Status.PASS,
                Severity.INFO,
                f"Same input twice gave cosine {det_cos:.6f} (~1.0).",
                {"cosine": det_cos, "threshold": _DET_MIN_COSINE},
            )
        )
    else:
        findings.append(
            _finding(
                "embed.determinism",
                "Embeddings are not deterministic",
                Status.WARN,
                Severity.MEDIUM,
                (
                    f"Same input twice gave cosine {det_cos:.6f}, below "
                    f"{_DET_MIN_COSINE}. A genuine embedding model is deterministic; "
                    f"drift suggests a noisy or re-wrapped backend."
                ),
                {"cosine": det_cos, "threshold": _DET_MIN_COSINE},
            )
        )

    # 4) Distinctness (different inputs -> cosine well below 1) -------------- #
    dist_cos = cosine_similarity(dist_a, dist_b)
    if dist_cos < _DISTINCT_MAX_COSINE:
        findings.append(
            _finding(
                "embed.distinctness",
                "Different inputs produce distinct vectors",
                Status.PASS,
                Severity.INFO,
                f"Unrelated texts gave cosine {dist_cos:.4f} (< {_DISTINCT_MAX_COSINE}).",
                {"cosine": dist_cos, "threshold": _DISTINCT_MAX_COSINE},
            )
        )
    else:
        findings.append(
            _finding(
                "embed.distinctness",
                "Different inputs produce near-identical vectors",
                Status.WARN,
                Severity.HIGH,
                (
                    f"Two unrelated texts gave cosine {dist_cos:.4f} "
                    f"(>= {_DISTINCT_MAX_COSINE}). Vectors that barely vary across "
                    f"inputs indicate a degenerate or stub embedding backend."
                ),
                {"cosine": dist_cos, "threshold": _DISTINCT_MAX_COSINE},
            )
        )

    # 5) Echoed model field ------------------------------------------------- #
    findings.append(
        _finding(
            "embed.model_field",
            "Echoed model field",
            Status.INFO,
            Severity.INFO,
            f"Relay reported model: {outcome.model_returned or '(none)'}.",
            {
                "model_returned": outcome.model_returned,
                "model_requested": target.model,
                "claimed_model": target.claimed_model,
            },
        )
    )

    return _verdict(findings, target)


# --------------------------------------------------------------------------- #
# Rerank audit (known-answer probe)
# --------------------------------------------------------------------------- #
async def audit_rerank(
    target: TargetConfig,
    query: str,
    documents: list[str],
    expected_top_index: int,
) -> dict[str, Any]:
    """Known-answer rerank probe.

    One document in ``documents`` is obviously the most relevant answer to
    ``query`` (at ``expected_top_index``). A genuine reranker ranks it first; we
    flag it if it does not. Returns the same small verdict dict shape.
    """
    findings: list[dict[str, Any]] = []

    async with make_client(target) as client:
        outcome, results = await client.rerank(query, documents)

    if not outcome.ok:
        findings.append(
            _finding(
                "rerank.connectivity",
                "Rerank endpoint unreachable",
                Status.ERROR,
                Severity.HIGH,
                f"POST /rerank failed: {outcome.error_message or outcome.status_code}",
                {"status_code": outcome.status_code, "error": outcome.error_message},
            )
        )
        return _verdict(findings, target)

    if not results:
        findings.append(
            _finding(
                "rerank.connectivity",
                "Rerank response empty",
                Status.ERROR,
                Severity.HIGH,
                "Endpoint returned no ranking results.",
                {"results": 0},
            )
        )
        return _verdict(findings, target)

    findings.append(
        _finding(
            "rerank.connectivity",
            "Rerank endpoint reachable",
            Status.PASS,
            Severity.INFO,
            f"Got {len(results)} ranked results.",
            {"results": len(results)},
        )
    )

    top = results[0]
    top_index = top["index"]
    ranking = [r["index"] for r in results]
    if top_index == expected_top_index:
        findings.append(
            _finding(
                "rerank.known_answer",
                "Reranker ranks the obvious answer first",
                Status.PASS,
                Severity.INFO,
                (
                    f"Document {expected_top_index} (the clearly-relevant one) ranked "
                    f"first with score {top['relevance_score']:.4f}."
                ),
                {"top_index": top_index, "expected": expected_top_index, "ranking": ranking},
            )
        )
    else:
        findings.append(
            _finding(
                "rerank.known_answer",
                "Reranker does NOT rank the obvious answer first",
                Status.WARN,
                Severity.HIGH,
                (
                    f"Expected document {expected_top_index} to rank first, but "
                    f"document {top_index} did. The reranker is not behaving as a "
                    f"genuine relevance model would."
                ),
                {"top_index": top_index, "expected": expected_top_index, "ranking": ranking},
            )
        )

    return _verdict(findings, target)
