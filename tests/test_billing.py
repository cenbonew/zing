"""Billing detector tests — token/usage inflation.

Inflation (the buyer-harmful direction) should FAIL; honest accounting should not.
"""

from __future__ import annotations

from zing.detectors.billing import BillingDetector
from zing.models import Severity, Status


async def test_billing_flags_gross_inflation(audit_context, mock_server):
    mock_server.inflate_usage_factor = 6.0  # 6x overbilling on prompt + completion
    result = await BillingDetector().run(audit_context)
    assert result.status == Status.FAIL
    assert any(f.severity == Severity.HIGH for f in result.findings)


async def test_billing_passes_honest_usage(audit_context, mock_server):
    mock_server.inflate_usage_factor = 1.0
    result = await BillingDetector().run(audit_context)
    assert result.status != Status.FAIL


async def test_billing_warns_on_missing_usage(audit_context, mock_server):
    mock_server.emit_usage = False
    result = await BillingDetector().run(audit_context)
    assert result.status == Status.WARN
    assert any(f.id == "billing.missing-usage" for f in result.findings)
