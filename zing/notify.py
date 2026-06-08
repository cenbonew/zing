"""Webhook alerting for scheduled re-audits.

`zing watch` re-runs an audit on an interval and, when the verdict crosses a risk
threshold (or regresses versus the previous saved run), POSTs a concise Chinese
alert to one or more webhooks. This module owns two concerns:

* **Formatting** — turning an :class:`~zing.models.AuditReport` dict into the
  body each chat platform expects (generic JSON, Slack, 飞书/Feishu, 钉钉/DingTalk).
  The text is intentionally compact: a single human-readable digest of risk,
  score, target, and the top key findings, with an optional "较上次" delta when a
  previous report is supplied.
* **Delivery** — :func:`send` auto-detects the platform from the webhook host
  (or takes an explicit ``kind``), POSTs via ``httpx`` (a core dependency), and
  returns whether it succeeded. It never raises: an unreachable webhook must not
  abort the watch loop.

Everything operates on plain report *dicts* (``AuditReport.model_dump()`` /
``json.loads(report.model_dump_json())``) so callers don't have to import the
pydantic models, and so a report loaded back from history works unchanged.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from zing.models import RiskLevel

# Ordering used to decide whether the current risk is *worse* than the previous
# run. INCONCLUSIVE deliberately sits below LOW: an inconclusive result is not a
# regression to alert on, but it's worse than a clean one.
_RISK_ORDER: dict[str, int] = {
    RiskLevel.CLEAN.value: 0,
    RiskLevel.INCONCLUSIVE.value: 1,
    RiskLevel.LOW.value: 2,
    RiskLevel.MEDIUM.value: 3,
    RiskLevel.HIGH.value: 4,
}

# Human-facing Chinese labels for each risk level.
_RISK_LABEL_ZH: dict[str, str] = {
    RiskLevel.CLEAN.value: "✅ 一致（CLEAN）",
    RiskLevel.LOW.value: "🔵 低风险（LOW）",
    RiskLevel.MEDIUM.value: "🟡 中风险（MEDIUM）",
    RiskLevel.HIGH.value: "🔴 高风险（HIGH）",
    RiskLevel.INCONCLUSIVE.value: "⚪ 无法判定（INCONCLUSIVE）",
}

_MAX_FINDINGS = 5


# --------------------------------------------------------------------------- #
# Risk helpers
# --------------------------------------------------------------------------- #
def _risk_value(report: dict[str, Any] | None) -> str:
    """The risk_level string of a report dict, defaulting to ``inconclusive``."""
    if not report:
        return RiskLevel.INCONCLUSIVE.value
    verdict = report.get("verdict") or {}
    risk = verdict.get("risk_level")
    if isinstance(risk, RiskLevel):
        return risk.value
    if isinstance(risk, str) and risk:
        return risk
    return RiskLevel.INCONCLUSIVE.value


def _risk_rank(risk: str) -> int:
    return _RISK_ORDER.get(risk, _RISK_ORDER[RiskLevel.INCONCLUSIVE.value])


def _risk_label(risk: str) -> str:
    return _RISK_LABEL_ZH.get(risk, risk)


def regressed(current: dict[str, Any], previous: dict[str, Any] | None) -> bool:
    """True if ``current``'s risk is strictly worse than ``previous``'s.

    Uses :data:`_RISK_ORDER` (clean < inconclusive < low < medium < high). With no
    previous run there is nothing to regress from, so this returns ``False``.
    """
    if previous is None:
        return False
    return _risk_rank(_risk_value(current)) > _risk_rank(_risk_value(previous))


def _delta_line(current: dict[str, Any], previous: dict[str, Any] | None) -> str | None:
    """A "较上次：…" line describing the risk change, or ``None`` if no previous."""
    if previous is None:
        return None
    cur = _risk_value(current)
    prev = _risk_value(previous)
    if cur == prev:
        return f"较上次：风险不变（{_risk_label(prev)}）"
    direction = "恶化 ⬆️" if _risk_rank(cur) > _risk_rank(prev) else "改善 ⬇️"
    return f"较上次：{_risk_label(prev)} → {_risk_label(cur)}（{direction}）"


# --------------------------------------------------------------------------- #
# Text digest
# --------------------------------------------------------------------------- #
def build_text(report: dict[str, Any], previous: dict[str, Any] | None = None) -> str:
    """A compact multi-line Chinese alert digest shared by every text platform.

    Includes the verdict (risk label + score + rating), the target (base_url and
    claimed model), the top key findings, and a "较上次" delta when a previous
    report is given.
    """
    report = report or {}
    verdict = report.get("verdict") or {}
    target = report.get("target") or {}

    risk = _risk_value(report)
    score = verdict.get("overall_score")
    rating = verdict.get("rating")
    base_url = target.get("base_url") or "—"
    claimed = target.get("claimed_model") or target.get("model") or "—"
    headline = verdict.get("headline") or ""

    score_part = "n/a" if score is None else f"{score}/100"
    if rating:
        score_part += f"（评级 {rating}）"

    lines = [
        "🛰️ zing 中继体检告警",
        f"风险：{_risk_label(risk)}",
        f"评分：{score_part}",
        f"目标：{base_url}",
        f"声称模型：{claimed}",
    ]
    if headline:
        lines.append(f"结论：{headline}")

    delta = _delta_line(report, previous)
    if delta:
        lines.append(delta)

    findings = [f for f in (verdict.get("key_findings") or []) if isinstance(f, str) and f.strip()]
    if findings:
        lines.append("主要发现：")
        for f in findings[:_MAX_FINDINGS]:
            lines.append(f"  • {f}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Per-platform payload formatters
# --------------------------------------------------------------------------- #
def format_generic(report: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """A structured JSON payload for a generic / custom webhook consumer."""
    report = report or {}
    verdict = report.get("verdict") or {}
    target = report.get("target") or {}
    return {
        "tool": "zing",
        "event": "audit_alert",
        "text": build_text(report, previous),
        "risk_level": _risk_value(report),
        "score": verdict.get("overall_score"),
        "rating": verdict.get("rating"),
        "headline": verdict.get("headline") or "",
        "base_url": target.get("base_url"),
        "claimed_model": target.get("claimed_model") or target.get("model"),
        "key_findings": [
            f for f in (verdict.get("key_findings") or []) if isinstance(f, str) and f.strip()
        ][:_MAX_FINDINGS],
        "previous_risk_level": _risk_value(previous) if previous is not None else None,
        "regressed": regressed(report, previous),
    }


def format_slack(report: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Slack incoming-webhook body — a single ``text`` field (mrkdwn)."""
    return {"text": build_text(report, previous)}


def format_feishu(report: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """飞书/Lark custom-bot body — ``msg_type: "text"`` with a ``content.text``."""
    return {"msg_type": "text", "content": {"text": build_text(report, previous)}}


def format_dingtalk(report: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """钉钉 custom-robot body — ``msgtype: "text"`` with a ``text.content``."""
    return {"msgtype": "text", "text": {"content": build_text(report, previous)}}


_FORMATTERS = {
    "generic": format_generic,
    "slack": format_slack,
    "feishu": format_feishu,
    "dingtalk": format_dingtalk,
}


# --------------------------------------------------------------------------- #
# Platform detection + delivery
# --------------------------------------------------------------------------- #
def detect_kind(webhook_url: str) -> str:
    """Guess the platform from the webhook host.

    hooks.slack.com → slack; feishu / larksuite → feishu; dingtalk → dingtalk;
    anything else → generic.
    """
    host = (urlparse(webhook_url).hostname or "").lower()
    if "hooks.slack.com" in host or host.endswith("slack.com"):
        return "slack"
    if "feishu" in host or "larksuite" in host or "larkoffice" in host:
        return "feishu"
    if "dingtalk" in host:
        return "dingtalk"
    return "generic"


def build_payload(
    report: dict[str, Any],
    *,
    kind: str = "auto",
    previous: dict[str, Any] | None = None,
    webhook_url: str | None = None,
) -> dict[str, Any]:
    """Pick the formatter by ``kind`` (or auto-detect from ``webhook_url``)."""
    resolved = kind
    if resolved == "auto":
        resolved = detect_kind(webhook_url or "")
    formatter = _FORMATTERS.get(resolved, format_generic)
    return formatter(report, previous)


async def send(
    webhook_url: str,
    report: dict[str, Any],
    *,
    kind: str = "auto",
    previous: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> bool:
    """POST an alert for ``report`` to ``webhook_url``; return whether it succeeded.

    The payload shape is chosen from ``kind`` (``slack`` | ``feishu`` | ``dingtalk``
    | ``generic``) or auto-detected from the URL host when ``kind == "auto"``.
    Never raises — a network error, timeout, or non-2xx status simply returns
    ``False`` so the watch loop keeps running.
    """
    if not webhook_url:
        return False
    payload = build_payload(report, kind=kind, previous=previous, webhook_url=webhook_url)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(webhook_url, json=payload)
        return resp.status_code < 400
    except Exception:
        return False
