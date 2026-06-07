"""Render an :class:`~zing.models.AuditReport` to JSON, Markdown, or HTML.

JSON is the canonical machine output (the model's own ``model_dump_json``, so its
field names match :mod:`zing.models` exactly). Markdown and HTML are derived,
human-facing views of the same data — they never invent facts the report does not
carry, and every relay/user string is escaped before it reaches HTML (via
:func:`_esc`) or Markdown (via :func:`_md`), so relay-controlled text cannot inject
markup, break tables, or spoof structure.

All three renderers are pure: they take a report and return a string. zing reports
black-box evidence of divergence and risk, not proof of fraud; the disclaimer in
each human view says so.
"""

from __future__ import annotations

import html
import json

from zing.models import AuditReport, RiskLevel, Severity, Status

_DISCLAIMER = (
    "zing performs black-box auditing. It gathers reproducible evidence of "
    "behavioral divergence and risk — not cryptographic proof of model identity "
    "or fraud. Treat findings as signals to investigate, review sample size and "
    "cost, and consider `zing compare` against a trusted baseline of the same "
    "declared model before drawing conclusions."
)

# Risk presentation, shared shape with the CLI's _RISK_STYLE.
_RISK_LABEL: dict[RiskLevel, str] = {
    RiskLevel.CLEAN: "CLEAN",
    RiskLevel.LOW: "LOW RISK",
    RiskLevel.MEDIUM: "MEDIUM RISK",
    RiskLevel.HIGH: "HIGH RISK",
    RiskLevel.INCONCLUSIVE: "INCONCLUSIVE",
}
_RISK_COLOR: dict[RiskLevel, str] = {
    RiskLevel.CLEAN: "#1a7f37",      # green
    RiskLevel.LOW: "#0969da",        # blue
    RiskLevel.MEDIUM: "#9a6700",     # amber
    RiskLevel.HIGH: "#cf222e",       # red
    RiskLevel.INCONCLUSIVE: "#656d76",  # grey
}

_SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.INFO: "ℹ️",
    Severity.LOW: "🟡",
    Severity.MEDIUM: "🟠",
    Severity.HIGH: "🔴",
    Severity.CRITICAL: "⛔",
}
_STATUS_EMOJI: dict[Status, str] = {
    Status.PASS: "✅",
    Status.WARN: "⚠️",
    Status.FAIL: "❌",
    Status.INCONCLUSIVE: "❔",
    Status.NOT_RUN: "➖",
    Status.INFO: "ℹ️",
    Status.ERROR: "💥",
}


# --------------------------------------------------------------------------- #
# Small shared formatters
# --------------------------------------------------------------------------- #
def _fmt_score(score: float | None) -> str:
    return "—" if score is None else f"{score:g}"


def _fmt_evidence_value(value: object) -> str:
    """Compact, single-line rendering of one evidence value for a highlight."""
    if isinstance(value, float):
        text = f"{value:.3g}"
    elif isinstance(value, (list, tuple)):
        items = ", ".join(_fmt_evidence_value(v) for v in list(value)[:6])
        if len(value) > 6:
            items += ", …"
        text = f"[{items}]"
    elif isinstance(value, dict):
        text = "{…}"
    else:
        text = str(value)
    text = " ".join(text.split())  # collapse whitespace/newlines
    return text if len(text) <= 160 else text[:157] + "…"


# Markdown metacharacters that let untrusted text break out of an inline context
# (tables, code spans, emphasis, links, raw HTML) or inject block structure.
_MD_ESCAPE = ("\\", "`", "|", "*", "_", "{", "}", "[", "]", "<", ">", "#")


def _md(value: object) -> str:
    """Escape a relay/user-controlled string for safe inline Markdown.

    Newlines are collapsed first so the text cannot escape its list item and inject
    a heading or table row; then Markdown metacharacters are backslash-escaped.
    """
    text = " ".join(str(value).split())
    for ch in _MD_ESCAPE:
        text = text.replace(ch, "\\" + ch)
    return text


def render_json(report: AuditReport) -> str:
    """Canonical machine output — field names match :mod:`zing.models` exactly."""
    return report.model_dump_json(indent=2)


# --------------------------------------------------------------------------- #
# Compact JSON — a lean, agent/LLM-facing view (no bulky evidence)
# --------------------------------------------------------------------------- #
def _compact_finding(dimension: str, finding) -> dict:
    out = {
        "dimension": dimension,
        "id": finding.id,
        "severity": finding.severity.value,
        "status": finding.status.value,
        "title": finding.title,
    }
    # Only the actionable findings carry their summary/recommendation; pass/info
    # findings stay one line so the payload stays small.
    if finding.status in (Status.WARN, Status.FAIL, Status.ERROR):
        if finding.summary:
            out["summary"] = finding.summary
        if finding.recommendation:
            out["recommendation"] = finding.recommendation
    return out


def compact_dict(report: AuditReport) -> dict:
    """A small, stable dict an agent can read without ingesting full evidence.

    ~5-10x smaller than the full report: verdict + per-dimension status + a flat
    findings list (severity/status/title, plus summary only for warn/fail/error).
    """
    v = report.verdict
    t = report.target
    target = {"name": t.name, "model": t.model, "base_url": t.base_url}
    if t.claimed_model:
        target["claimed_model"] = t.claimed_model
    if t.declared_provider:
        target["provider"] = t.declared_provider

    rel = None
    if report.reliability:
        r = report.reliability
        rel = {
            "requests": r.requests,
            "successes": r.successes,
            "success_rate": round(r.success_rate, 4),
            "rate_limited": r.rate_limited,
            "latency_p95_ms": r.latency_ms.get("p95"),
        }

    return {
        "tool": "zing",
        "version": report.tool_version,
        "generated_at": report.generated_at,
        "mode": report.mode,
        "suite": report.suite,
        "target": target,
        "baseline": (
            {"model": report.baseline.model, "base_url": report.baseline.base_url}
            if report.baseline
            else None
        ),
        "verdict": {
            "risk": v.risk_level.value,
            "score": v.overall_score,
            "rating": v.rating,
            "confidence": v.confidence,
            "headline": v.headline,
            "summary": v.summary,
            "key_findings": v.key_findings,
        },
        "dimensions": {
            d.dimension.value: {"score": d.score, "status": d.status.value}
            for d in report.dimensions
            if d.status != Status.NOT_RUN
        },
        "findings": [
            _compact_finding(det.dimension.value, f)
            for det in report.detectors
            for f in det.findings
        ],
        "reliability": rel,
        "judge_used": report.judge_used,
        "warnings": report.warnings or [],
    }


def render_compact(report: AuditReport) -> str:
    """Lean JSON for agents/LLMs — verdict + findings without bulky evidence."""
    return json.dumps(compact_dict(report), ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def render_markdown(report: AuditReport) -> str:
    v = report.verdict
    lines: list[str] = []

    risk_label = _RISK_LABEL.get(v.risk_level, v.risk_level.value)
    score = _fmt_score(v.overall_score)
    rating = v.rating or "—"
    lines.append(f"# zing audit — {report.target.model}")
    lines.append("")
    lines.append(
        f"**Risk:** {risk_label}  ·  **Score:** {score}/100  ·  "
        f"**Rating:** {rating}  ·  **Confidence:** {v.confidence}"
    )
    lines.append("")

    # Metadata line.
    meta = [
        f"mode `{report.mode}`",
        f"suite `{report.suite}`",
        f"target `{report.target.name}` → `{report.target.base_url}`",
    ]
    if report.target.declared_provider:
        meta.append(f"provider `{report.target.declared_provider}`")
    if report.baseline:
        meta.append(f"baseline `{report.baseline.model}`")
    if report.judge_used:
        meta.append(f"judge `{report.judge_model or 'on'}`")
    if report.generated_at:
        meta.append(f"generated `{report.generated_at}`")
    lines.append("  ·  ".join(meta))
    lines.append(f"\n_zing v{report.tool_version}_")
    lines.append("")

    # Verdict.
    lines.append("## Verdict")
    lines.append("")
    if v.headline:
        lines.append(f"**{v.headline}**")
        lines.append("")
    if v.summary:
        lines.append(_md(v.summary))
        lines.append("")
    if v.key_findings:
        lines.append("**Key findings**")
        lines.append("")
        for kf in v.key_findings:
            lines.append(f"- {_md(kf)}")
        lines.append("")

    # Dimensions table.
    lines.append("## Dimensions")
    lines.append("")
    if report.dimensions:
        lines.append("| Dimension | Score | Weight | Status |")
        lines.append("| --- | ---: | ---: | --- |")
        for d in report.dimensions:
            emoji = _STATUS_EMOJI.get(d.status, "")
            lines.append(
                f"| {d.dimension.value} | {_fmt_score(d.score)} | "
                f"{d.weight:g} | {emoji} {d.status.value} |"
            )
    else:
        lines.append("_No dimensions scored._")
    lines.append("")

    # Findings grouped per detector.
    lines.append("## Findings")
    lines.append("")
    detector_groups = [("Target", report.detectors)]
    if report.baseline_detectors:
        detector_groups.append(("Baseline", report.baseline_detectors))

    for group_label, detectors in detector_groups:
        if len(detector_groups) > 1:
            lines.append(f"### {group_label}")
            lines.append("")
        if not detectors:
            lines.append("_No detectors ran._")
            lines.append("")
            continue
        for det in detectors:
            det_emoji = _STATUS_EMOJI.get(det.status, "")
            score_txt = _fmt_score(det.score)
            lines.append(
                f"### {det_emoji} {det.name} "
                f"(`{det.id}` · {det.dimension.value} · score {score_txt})"
            )
            lines.append("")
            if det.error:
                lines.append(f"> Detector error: {_md(det.error)}")
                lines.append("")
            if not det.findings:
                lines.append("_No findings._")
                lines.append("")
                continue
            for f in det.findings:
                sev = _SEVERITY_EMOJI.get(f.severity, "")
                stat = _STATUS_EMOJI.get(f.status, "")
                lines.append(
                    f"- {stat} **{_md(f.title)}** — {sev} {f.severity.value} / {f.status.value}"
                )
                if f.summary:
                    lines.append(f"  - {_md(f.summary)}")
                for key, value in list(f.evidence.items())[:8]:
                    lines.append(f"  - `{_md(key)}`: {_md(_fmt_evidence_value(value))}")
                if f.recommendation:
                    lines.append(f"  - _Recommendation: {_md(f.recommendation)}_")
            lines.append("")

    # Reliability.
    if report.reliability:
        r = report.reliability
        lines.append("## Reliability")
        lines.append("")
        lines.append(
            f"- Requests: {r.successes}/{r.requests} succeeded "
            f"({r.success_rate * 100:.0f}%)"
        )
        if r.rate_limited:
            lines.append(f"- Rate-limited (429): {r.rate_limited} (excluded from success rate)")
        if r.latency_ms:
            parts = [
                f"{k} {v:.0f} ms"
                for k, v in r.latency_ms.items()
                if v is not None
            ]
            if parts:
                lines.append(f"- Latency: {', '.join(parts)}")
        if r.errors:
            errs = ", ".join(f"{k}: {n}" for k, n in r.errors.items())
            lines.append(f"- Errors: {errs}")
        lines.append("")

    # Notes & warnings.
    if report.notes:
        lines.append("## Notes")
        lines.append("")
        for n in report.notes:
            lines.append(f"- {n}")
        lines.append("")
    if report.warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in report.warnings:
            lines.append(f"- ⚠️ {_md(w)}")
        lines.append("")

    # Disclaimer footer.
    lines.append("---")
    lines.append("")
    lines.append(f"> **Disclaimer.** {_DISCLAIMER}")
    lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


_STATUS_CLASS: dict[Status, str] = {
    Status.PASS: "pass",
    Status.WARN: "warn",
    Status.FAIL: "fail",
    Status.INCONCLUSIVE: "muted",
    Status.NOT_RUN: "muted",
    Status.INFO: "info",
    Status.ERROR: "fail",
}


def render_html(report: AuditReport) -> str:
    v = report.verdict
    risk_label = _RISK_LABEL.get(v.risk_level, v.risk_level.value)
    risk_color = _RISK_COLOR.get(v.risk_level, "#656d76")
    score = _fmt_score(v.overall_score)
    rating = v.rating or "—"

    out: list[str] = []
    out.append("<!DOCTYPE html>")
    out.append('<html lang="en"><head><meta charset="utf-8">')
    out.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    out.append(f"<title>zing audit — {_esc(report.target.model)}</title>")
    out.append(_CSS)
    out.append("</head><body><main>")

    # Header + risk badge.
    out.append('<header class="report-head">')
    out.append(f"<h1>zing audit <span class=\"model\">{_esc(report.target.model)}</span></h1>")
    out.append(
        f'<div class="badge" style="background:{risk_color}">{_esc(risk_label)}</div>'
    )
    out.append('<div class="scoreline">')
    out.append(f"<span><strong>{_esc(score)}</strong>/100</span>")
    out.append(f"<span>rating <strong>{_esc(rating)}</strong></span>")
    out.append(f"<span>confidence <strong>{_esc(v.confidence)}</strong></span>")
    out.append("</div>")
    # Metadata.
    meta_bits = [
        f"mode <code>{_esc(report.mode)}</code>",
        f"suite <code>{_esc(report.suite)}</code>",
        f"target <code>{_esc(report.target.name)}</code> → <code>{_esc(report.target.base_url)}</code>",
    ]
    if report.target.declared_provider:
        meta_bits.append(f"provider <code>{_esc(report.target.declared_provider)}</code>")
    if report.baseline:
        meta_bits.append(f"baseline <code>{_esc(report.baseline.model)}</code>")
    if report.judge_used:
        meta_bits.append(f"judge <code>{_esc(report.judge_model or 'on')}</code>")
    if report.generated_at:
        meta_bits.append(f"generated <code>{_esc(report.generated_at)}</code>")
    out.append(f'<p class="meta">{" · ".join(meta_bits)}</p>')
    out.append(f'<p class="meta">zing v{_esc(report.tool_version)}</p>')
    out.append("</header>")

    # Verdict.
    out.append('<section class="card">')
    out.append("<h2>Verdict</h2>")
    if v.headline:
        out.append(f"<p class=\"headline\">{_esc(v.headline)}</p>")
    if v.summary:
        out.append(f"<p>{_esc(v.summary)}</p>")
    if v.key_findings:
        out.append("<h3>Key findings</h3><ul>")
        for kf in v.key_findings:
            out.append(f"<li>{_esc(kf)}</li>")
        out.append("</ul>")
    out.append("</section>")

    # Dimensions table.
    out.append('<section class="card"><h2>Dimensions</h2>')
    if report.dimensions:
        out.append('<table><thead><tr>'
                    "<th>Dimension</th><th class=\"num\">Score</th>"
                    "<th class=\"num\">Weight</th><th>Status</th>"
                    "</tr></thead><tbody>")
        for d in report.dimensions:
            cls = _STATUS_CLASS.get(d.status, "muted")
            out.append(
                "<tr>"
                f"<td>{_esc(d.dimension.value)}</td>"
                f'<td class="num">{_esc(_fmt_score(d.score))}</td>'
                f'<td class="num">{_esc(f"{d.weight:g}")}</td>'
                f'<td><span class="pill {cls}">{_esc(d.status.value)}</span></td>'
                "</tr>"
            )
        out.append("</tbody></table>")
    else:
        out.append("<p class=\"muted\">No dimensions scored.</p>")
    out.append("</section>")

    # Findings per detector.
    detector_groups = [("Target", report.detectors)]
    if report.baseline_detectors:
        detector_groups.append(("Baseline", report.baseline_detectors))

    out.append('<section class="card"><h2>Findings</h2>')
    for group_label, detectors in detector_groups:
        if len(detector_groups) > 1:
            out.append(f"<h3 class=\"group\">{_esc(group_label)}</h3>")
        if not detectors:
            out.append('<p class="muted">No detectors ran.</p>')
            continue
        for det in detectors:
            det_cls = _STATUS_CLASS.get(det.status, "muted")
            out.append('<div class="detector">')
            out.append(
                '<h3 class="det-title">'
                f"{_esc(det.name)} "
                f'<span class="pill {det_cls}">{_esc(det.status.value)}</span> '
                f'<span class="tag">{_esc(det.id)}</span> '
                f'<span class="tag">{_esc(det.dimension.value)}</span> '
                f'<span class="tag">score {_esc(_fmt_score(det.score))}</span>'
                "</h3>"
            )
            if det.error:
                out.append(f'<p class="fail">Detector error: {_esc(det.error)}</p>')
            if not det.findings:
                out.append('<p class="muted">No findings.</p>')
            for f in det.findings:
                f_cls = _STATUS_CLASS.get(f.status, "muted")
                sev = _esc(f.severity.value)
                out.append('<div class="finding">')
                out.append(
                    '<p class="f-head">'
                    f'<span class="pill {f_cls}">{_esc(f.status.value)}</span> '
                    f'<span class="sev sev-{sev}">{sev}</span> '
                    f"<strong>{_esc(f.title)}</strong></p>"
                )
                if f.summary:
                    out.append(f"<p>{_esc(f.summary)}</p>")
                if f.evidence:
                    out.append('<table class="evidence"><tbody>')
                    for key, value in list(f.evidence.items())[:12]:
                        out.append(
                            "<tr>"
                            f"<td class=\"ekey\">{_esc(key)}</td>"
                            f"<td><code>{_esc(_fmt_evidence_value(value))}</code></td>"
                            "</tr>"
                        )
                    out.append("</tbody></table>")
                if f.recommendation:
                    out.append(f'<p class="rec">Recommendation: {_esc(f.recommendation)}</p>')
                out.append("</div>")
            out.append("</div>")
    out.append("</section>")

    # Reliability.
    if report.reliability:
        r = report.reliability
        out.append('<section class="card"><h2>Reliability</h2><ul>')
        out.append(
            f"<li>Requests: {r.successes}/{r.requests} succeeded "
            f"({r.success_rate * 100:.0f}%)</li>"
        )
        if r.rate_limited:
            out.append(
                f"<li>Rate-limited (429): {r.rate_limited} (excluded from success rate)</li>"
            )
        if r.latency_ms:
            parts = [
                f"{_esc(k)} {v:.0f} ms"
                for k, v in r.latency_ms.items()
                if v is not None
            ]
            if parts:
                out.append(f"<li>Latency: {', '.join(parts)}</li>")
        if r.errors:
            errs = ", ".join(f"{_esc(k)}: {n}" for k, n in r.errors.items())
            out.append(f"<li>Errors: {errs}</li>")
        out.append("</ul></section>")

    # Notes & warnings.
    if report.notes:
        out.append('<section class="card"><h2>Notes</h2><ul>')
        for n in report.notes:
            out.append(f"<li>{_esc(n)}</li>")
        out.append("</ul></section>")
    if report.warnings:
        out.append('<section class="card warnings"><h2>Warnings</h2><ul>')
        for w in report.warnings:
            out.append(f"<li>{_esc(w)}</li>")
        out.append("</ul></section>")

    # Disclaimer.
    out.append(f'<footer class="disclaimer"><strong>Disclaimer.</strong> {_esc(_DISCLAIMER)}</footer>')
    out.append("</main></body></html>")

    return "".join(out)


_CSS = """<style>
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1rem;
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  color: #1f2328; background: #f6f8fa;
}
main { max-width: 920px; margin: 0 auto; }
h1 { font-size: 1.7rem; margin: 0 0 .25rem; }
h1 .model { font-weight: 500; color: #57606a; }
h2 { font-size: 1.2rem; margin: 0 0 .75rem; border-bottom: 1px solid #d0d7de; padding-bottom: .35rem; }
h3 { font-size: 1rem; margin: 1rem 0 .5rem; }
h3.group { color: #57606a; text-transform: uppercase; letter-spacing: .04em; font-size: .8rem; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .85em;
  background: #eaeef2; padding: .1em .35em; border-radius: 4px; }
.report-head { margin-bottom: 1.5rem; }
.badge { display: inline-block; color: #fff; font-weight: 700; letter-spacing: .03em;
  padding: .35rem .9rem; border-radius: 999px; font-size: .9rem; margin: .25rem 0 .75rem; }
.scoreline { display: flex; gap: 1.5rem; flex-wrap: wrap; font-size: 1.05rem; margin-bottom: .5rem; }
.scoreline strong { font-size: 1.15rem; }
.meta { color: #57606a; font-size: .85rem; margin: .2rem 0; }
.card { background: #fff; border: 1px solid #d0d7de; border-radius: 10px;
  padding: 1.1rem 1.25rem; margin-bottom: 1.25rem; }
.card.warnings { border-color: #d4a72c; background: #fff8e6; }
.headline { font-size: 1.1rem; font-weight: 600; margin: 0 0 .5rem; }
table { width: 100%; border-collapse: collapse; font-size: .92rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid #eaeef2; vertical-align: top; }
th { color: #57606a; font-weight: 600; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.pill { display: inline-block; padding: .05rem .5rem; border-radius: 999px;
  font-size: .78rem; font-weight: 600; text-transform: uppercase; letter-spacing: .02em; }
.pill.pass { background: #dafbe1; color: #1a7f37; }
.pill.warn { background: #fff1c2; color: #9a6700; }
.pill.fail { background: #ffebe9; color: #cf222e; }
.pill.info { background: #ddf4ff; color: #0969da; }
.pill.muted { background: #eaeef2; color: #57606a; }
.muted { color: #57606a; }
.fail { color: #cf222e; }
.tag { display: inline-block; background: #eaeef2; color: #57606a; border-radius: 4px;
  padding: .05rem .4rem; font-size: .75rem; font-weight: 500; margin-left: .25rem; }
.detector { border-top: 1px solid #eaeef2; padding-top: .75rem; margin-top: .75rem; }
.detector:first-of-type { border-top: 0; padding-top: 0; margin-top: 0; }
.det-title { display: flex; align-items: center; flex-wrap: wrap; gap: .3rem; }
.finding { background: #f6f8fa; border: 1px solid #eaeef2; border-radius: 8px;
  padding: .6rem .75rem; margin: .5rem 0; }
.f-head { margin: 0 0 .35rem; }
.sev { font-size: .75rem; font-weight: 700; text-transform: uppercase; }
.sev-info { color: #57606a; } .sev-low { color: #9a6700; } .sev-medium { color: #bc4c00; }
.sev-high { color: #cf222e; } .sev-critical { color: #82071e; }
table.evidence { font-size: .85rem; margin: .35rem 0; }
table.evidence td { border-bottom: 1px solid #eaeef2; padding: .25rem .5rem; }
td.ekey { color: #57606a; white-space: nowrap; font-weight: 500; }
.rec { font-style: italic; color: #57606a; margin: .35rem 0 0; }
.disclaimer { color: #57606a; font-size: .85rem; border-top: 1px solid #d0d7de;
  padding-top: 1rem; margin-top: 1.5rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e6edf3; background: #0d1117; }
  h1 .model, .meta, th, .muted, td.ekey, .rec, .disclaimer { color: #8b949e; }
  code, .tag { background: #21262d; color: #c9d1d9; }
  h2 { border-color: #30363d; }
  .card { background: #161b22; border-color: #30363d; }
  .card.warnings { background: #2d2206; border-color: #9a6700; }
  th, td, .detector, table.evidence td, .finding { border-color: #21262d; }
  .finding { background: #0d1117; }
  .pill.pass { background: #12361f; color: #3fb950; }
  .pill.warn { background: #3a2d04; color: #d29922; }
  .pill.fail { background: #3a1417; color: #f85149; }
  .pill.info { background: #0b2942; color: #58a6ff; }
  .pill.muted { background: #21262d; color: #8b949e; }
}
</style>"""
