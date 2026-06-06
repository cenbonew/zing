"""End-to-end tests for the ConnectivityDetector against the mock relay."""

from __future__ import annotations

from zing.detectors.connectivity import ConnectivityDetector
from zing.models import Dimension, Status


async def test_connectivity_honest_relay_passes(audit_context, mock_server):
    # Echo the connectivity canary exactly so the basic-completion check passes.
    mock_server.model_ids = ["gpt-4o", "gpt-4o-mini"]
    result = await ConnectivityDetector().run(audit_context)

    assert result.dimension == Dimension.CONNECTIVITY
    assert result.status == Status.PASS
    assert result.score is not None and result.score >= 85.0

    by_id = {f.id: f for f in result.findings}
    assert by_id["connectivity.models"].status == Status.PASS
    assert by_id["connectivity.chat"].status == Status.PASS
    # The claimed model appears in the listed ids.
    assert by_id["connectivity.models"].evidence["claimed_listed"] is True
    # The connectivity canary was echoed back by the mock.
    assert by_id["connectivity.chat"].evidence["canary_echoed"] is True
    assert result.evidence["model_ids_sample"] == ["gpt-4o", "gpt-4o-mini"]


async def test_connectivity_models_unreachable_warns_but_chat_still_pass(audit_context, mock_server):
    # Some relays disable /v1/models; that is a WARN, not a hard failure.
    mock_server.models_status = 404
    result = await ConnectivityDetector().run(audit_context)

    by_id = {f.id: f for f in result.findings}
    assert by_id["connectivity.models"].status == Status.WARN
    # Basic completion still works, so the detector overall passes.
    assert by_id["connectivity.chat"].status == Status.PASS
    assert result.status == Status.PASS


async def test_connectivity_chat_failure_fails(audit_context, mock_server):
    mock_server.chat_status = 500
    mock_server.error_body = {"error": {"message": "internal error"}}
    result = await ConnectivityDetector().run(audit_context)

    by_id = {f.id: f for f in result.findings}
    assert by_id["connectivity.chat"].status == Status.FAIL
    assert result.status == Status.FAIL
    # /models passed (100) but chat failed (0): the average drags well below PASS.
    assert result.score is not None and result.score <= 50.0


async def test_connectivity_unreachable_endpoint_fails(audit_context, mock_server):
    # Everything is down: models 503 and chat 503.
    mock_server.models_status = 503
    mock_server.chat_status = 503
    result = await ConnectivityDetector().run(audit_context)
    assert result.status == Status.FAIL
