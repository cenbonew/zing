"""zing command-line interface.

Commands:
  zing init      write a starter config file
  zing check     audit one relay endpoint
  zing compare   audit a relay against a trusted baseline of the same model
  zing models    quickly probe an endpoint's /models list
  zing kb        inspect the bundled knowledge base
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from zing import __version__
from zing.clients import make_client
from zing.config import (
    TEMPLATE,
    AuditOptions,
    ConfigError,
    build_target,
    load_config_file,
    merge_headers,
    section,
    validate_format,
    validate_risk,
    validate_suite,
)
from zing.knowledge import load_knowledge_base
from zing.models import AuditReport, RiskLevel, Status, TargetConfig

# Risk ordering for the `watch` alert threshold (clean < low < medium < high).
_WATCH_RISK_RANK = {
    RiskLevel.CLEAN: 0,
    RiskLevel.INCONCLUSIVE: 1,
    RiskLevel.LOW: 2,
    RiskLevel.MEDIUM: 3,
    RiskLevel.HIGH: 4,
}

app = typer.Typer(
    name="zing",
    help=(
        "LLM relay reality check — audit whether a relay serves the model it claims "
        "(货不对板检测).\n\n"
        "For agents / scripts: add --compact for a lean JSON verdict (or --json for the "
        "full report), --dry-run to estimate API calls first, and --fail-on-risk "
        "low|medium|high to gate on exit code. `kb --json` / `models --json` give "
        "machine-readable discovery."
    ),
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)

_RISK_STYLE = {
    RiskLevel.CLEAN: ("green", "✓ CLEAN"),
    RiskLevel.LOW: ("cyan", "• LOW RISK"),
    RiskLevel.MEDIUM: ("yellow", "▲ MEDIUM RISK"),
    RiskLevel.HIGH: ("bold red", "✗ HIGH RISK"),
    RiskLevel.INCONCLUSIVE: ("dim", "? INCONCLUSIVE"),
}
_STATUS_STYLE = {
    Status.PASS: "green",
    Status.WARN: "yellow",
    Status.FAIL: "red",
    Status.INCONCLUSIVE: "dim",
    Status.NOT_RUN: "dim",
    Status.INFO: "blue",
    Status.ERROR: "red",
}
_RISK_ORDER = [
    RiskLevel.CLEAN,
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _print_summary(report: AuditReport, written: list[Path]) -> None:
    v = report.verdict
    style, label = _RISK_STYLE.get(v.risk_level, ("white", v.risk_level.value))
    score = "n/a" if v.overall_score is None else f"{v.overall_score}/100 (rating {v.rating})"
    head = f"[{style}]{label}[/]  —  {v.headline}"
    body = (
        f"Target : {report.target.name}  ·  model [bold]{report.target.model}[/]"
        f"{'  ·  provider ' + report.target.declared_provider if report.target.declared_provider else ''}\n"
        f"Mode   : {report.mode}  ·  suite {report.suite}"
        f"{'  ·  baseline ' + report.baseline.model if report.baseline else ''}"
        f"{'  ·  judge ' + (report.judge_model or 'on') if report.judge_used else ''}\n"
        f"Score  : {score}  ·  confidence {v.confidence}\n\n"
        f"{v.summary}"
    )
    console.print(Panel(body, title=head, border_style=style.split()[-1]))

    table = Table(title="Dimensions", show_lines=False, expand=False)
    table.add_column("Dimension")
    table.add_column("Score", justify="right")
    table.add_column("Weight", justify="right")
    table.add_column("Status")
    for d in report.dimensions:
        sc = "—" if d.score is None else f"{d.score}"
        st_style = _STATUS_STYLE.get(d.status, "white")
        table.add_row(
            d.dimension.value,
            sc,
            f"{d.weight:.0f}",
            f"[{st_style}]{d.status.value}[/]",
        )
    console.print(table)

    if v.key_findings:
        console.print("\n[bold]Key findings[/bold]")
        for kf in v.key_findings:
            console.print(f"  • {kf}")

    # Detector findings worth surfacing (warn/fail/error or high severity).
    notable = [
        (det, f)
        for det in report.detectors
        for f in det.findings
        if f.status in (Status.WARN, Status.FAIL, Status.ERROR)
    ]
    if notable:
        console.print("\n[bold]Detector findings[/bold]")
        for det, f in notable[:14]:
            fstyle = _STATUS_STYLE.get(f.status, "white")
            console.print(
                f"  [{fstyle}]{f.status.value:5s}[/] [{f.severity.value}] "
                f"{det.dimension.value}: {f.title}"
            )

    if report.reliability:
        r = report.reliability
        p95 = r.latency_ms.get("p95")
        console.print(
            f"\n[bold]Reliability[/bold]: {r.successes}/{r.requests} ok "
            f"({r.success_rate * 100:.0f}%)"
            + (f", p95 {p95:.0f} ms" if p95 else "")
        )

    if report.warnings:
        console.print("\n[yellow]Warnings[/yellow]")
        for w in report.warnings:
            console.print(f"  ! {w}")

    if written:
        console.print("\n[bold]Reports[/bold]")
        for p in written:
            console.print(f"  → {p}")

    console.print(
        "\n[dim]zing reports black-box evidence of divergence and risk, "
        "not proof of fraud. Use `zing compare` for the strongest verdict.[/dim]"
    )


def _exit_code(report: AuditReport, fail_under: float | None, fail_on_risk: str | None) -> int:
    v = report.verdict
    if fail_under is not None and v.overall_score is not None and v.overall_score < fail_under:
        return 1
    if fail_on_risk:
        try:
            threshold = RiskLevel(fail_on_risk)
        except ValueError:
            return 0
        if (
            threshold in _RISK_ORDER
            and v.risk_level in _RISK_ORDER
            and _RISK_ORDER.index(v.risk_level) >= _RISK_ORDER.index(threshold)
        ):
            return 1
    return 0


# --------------------------------------------------------------------------- #
# Shared option resolution
# --------------------------------------------------------------------------- #
def _target_from(
    cfg: dict,
    sect: str,
    *,
    kind: str,
    name: str | None,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    declared_provider: str | None,
    timeout: float | None,
    headers: list[str] | None,
    api: str | None = None,
    claimed_model: str | None = None,
) -> TargetConfig:
    fs = section(cfg, sect)
    merged_headers = merge_headers(fs.get("headers"), headers)
    return build_target(
        kind=kind,
        name=name or fs.get("name"),
        base_url=base_url or fs.get("base_url"),
        api_key=api_key or fs.get("api_key"),
        model=model or fs.get("model"),
        declared_provider=declared_provider or fs.get("declared_provider"),
        timeout_sec=timeout if timeout is not None else fs.get("timeout_sec"),
        headers=merged_headers,
        api=api or fs.get("api"),
        claimed_model=claimed_model or fs.get("claimed_model"),
    )


def _build_options(cfg: dict, **overrides) -> AuditOptions:
    run_cfg = section(cfg, "run")

    def pick(key: str, cfg_key: str, default):
        """CLI override (when not None) wins over the config-file value."""
        val = overrides.get(key)
        return val if val is not None else run_cfg.get(cfg_key, default)

    opts = AuditOptions(
        suite=validate_suite(overrides.get("suite") or run_cfg.get("suite") or "standard"),
        judge=bool(pick("judge", "judge", False)),
        only=overrides.get("only") or [],
        skip=overrides.get("skip") or [],
        reliability_requests=int(pick("reliability_requests", "reliability_requests", 8)),
        reliability_concurrency=int(pick("concurrency", "concurrency", 3)),
        max_context_probe_tokens=int(pick("max_context_tokens", "max_context_probe_tokens", 200_000)),
    )
    return opts


def _judge_target(cfg: dict, base_url, api_key, model, baseline: TargetConfig | None):
    """Resolve a judge endpoint from flags/config, falling back to the baseline."""
    js = section(cfg, "judge")
    b = base_url or js.get("base_url")
    m = model or js.get("model")
    k = api_key or js.get("api_key")
    if b and m:
        return build_target(
            kind="judge", name="judge", base_url=b, api_key=k, model=m, timeout_sec=None, headers={}
        )
    return baseline


def _machine_mode(as_json: bool, as_compact: bool) -> bool:
    """True when output should be pure JSON to stdout (no files, no rich panel)."""
    return as_json or as_compact


def _emit_machine_error(exc: Exception, *, error_type: str = "config_error") -> None:
    """Print a parseable error object to stdout and exit 2 (for agents/CI)."""
    print(json.dumps({"error": {"type": error_type, "message": str(exc)}}, ensure_ascii=False))
    raise typer.Exit(code=2)


def _build_dry_run_plan(target, options, baseline, judge_target, mode: str) -> dict:
    """What `zing check/compare` WOULD do — selected detectors and an API-call
    estimate — without issuing a single request, so an agent can budget cost."""
    import zing.detectors  # noqa: F401  -- populate the registry
    from zing.detectors.base import select_detectors

    has_judge = bool(options.judge and (judge_target or baseline))
    has_baseline = baseline is not None
    detectors = select_detectors(
        options.suite, has_judge=has_judge, has_baseline=has_baseline, enabled=options.enabled
    )
    rows: list[dict] = []
    total = 0
    for d in detectors:
        calls = options.reliability_requests if d.id == "reliability" else d.cost_hint
        rows.append(
            {"id": d.id, "dimension": d.dimension.value, "min_suite": d.min_suite, "est_calls": calls}
        )
        total += calls
    if has_baseline:
        total = int(total * 1.4)  # compare-mode detectors also probe the baseline
    return {
        "tool": "zing",
        "version": __version__,
        "dry_run": True,
        "mode": mode,
        "suite": options.suite,
        "target": {
            "model": target.model,
            "claimed_model": target.claimed_model,
            "base_url": target.base_url,
            "provider": target.declared_provider,
        },
        "baseline": ({"model": baseline.model, "base_url": baseline.base_url} if baseline else None),
        "judge": has_judge,
        "detectors": rows,
        "estimated_api_calls": total,
        "note": (
            "Rough upper bound; reliability uses --reliability-requests. "
            "No API calls were made."
        ),
    }


def _emit_dry_run(plan: dict, *, machine: bool) -> None:
    if machine:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        raise typer.Exit(code=0)
    console.print(
        Panel(
            f"[bold]Dry run[/] · mode {plan['mode']} · suite {plan['suite']} · "
            f"model [bold]{plan['target']['model']}[/]\n"
            f"Would run [bold]{len(plan['detectors'])}[/] detectors, "
            f"~[bold]{plan['estimated_api_calls']}[/] API calls. No requests were made.",
            border_style="cyan",
        )
    )
    table = Table(show_lines=False)
    table.add_column("Detector")
    table.add_column("Dimension")
    table.add_column("Min suite")
    table.add_column("~calls", justify="right")
    for d in plan["detectors"]:
        table.add_row(d["id"], d["dimension"], d["min_suite"], str(d["est_calls"]))
    console.print(table)
    raise typer.Exit(code=0)


def _run_and_report(
    target,
    options,
    *,
    baseline=None,
    judge_target=None,
    mode,
    out_dir,
    fmt,
    fail_under,
    fail_on_risk,
    as_json,
    as_compact,
    kb_dirs,
) -> None:
    # Imported here so a partially-built report module never breaks `zing kb` etc.
    from zing.report import render_compact, write_reports
    from zing.runner import run_audit

    command = "zing " + " ".join(sys.argv[1:])
    report = asyncio.run(
        run_audit(
            target,
            options,
            baseline=baseline,
            judge_target=judge_target,
            mode=mode,
            command=command,
            kb_dirs=kb_dirs,
        )
    )

    if as_compact:
        # Lean, agent/LLM-facing JSON (verdict + findings, no bulky evidence).
        print(render_compact(report))
    elif as_json:
        # Full machine-facing report.
        print(report.model_dump_json(indent=2))
    else:
        written = write_reports(report, out_dir=out_dir, fmt=validate_format(fmt))
        _print_summary(report, written)

    raise typer.Exit(code=_exit_code(report, fail_under, fail_on_risk))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
@app.command("init")
def init_config(
    path: Annotated[Path, typer.Option("--path", "-p", help="Where to write the config.")] = Path("zing.yaml"),
    force: Annotated[bool, typer.Option("--force", help="Overwrite if it exists.")] = False,
) -> None:
    """Write a starter zing.yaml config."""
    if path.exists() and not force:
        err_console.print(f"[red]{path} already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(code=2)
    path.write_text(TEMPLATE, encoding="utf-8")
    console.print(f"Wrote {path}")


@app.command("check")
def check_command(
    config: Annotated[Path | None, typer.Option("--config", "-c", help="YAML config path.")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Relay base URL, e.g. https://relay.example.com/v1.")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key, or env:VAR / file:/path reference.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Model id actually sent in requests.")] = None,
    claimed_model: Annotated[str | None, typer.Option("--claimed-model", help="Model the relay claims to serve, if different from --model (audits the real model against this profile).")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Display name for the target.")] = None,
    api: Annotated[str | None, typer.Option("--api", help="Wire protocol: auto | openai | anthropic.")] = None,
    declared_provider: Annotated[str | None, typer.Option("--declared-provider", help="Provider hint for KB lookup (openai, anthropic, deepseek, ...).")] = None,
    header: Annotated[list[str] | None, typer.Option("--header", "-H", help="Extra header 'Name: value' (repeatable).")] = None,
    suite: Annotated[str | None, typer.Option("--suite", help="smoke | standard | deep | full.")] = None,
    judge: Annotated[bool | None, typer.Option("--judge/--no-judge", help="Enable code+LLM hybrid judging.")] = None,
    judge_base_url: Annotated[str | None, typer.Option("--judge-base-url", help="Trusted judge endpoint base URL.")] = None,
    judge_api_key: Annotated[str | None, typer.Option("--judge-api-key", help="Judge API key (env:VAR ok).")] = None,
    judge_model: Annotated[str | None, typer.Option("--judge-model", help="Judge model id.")] = None,
    only: Annotated[list[str] | None, typer.Option("--only", help="Run only these detector ids (repeatable).")] = None,
    skip: Annotated[list[str] | None, typer.Option("--skip", help="Skip these detector ids (repeatable).")] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="Report output directory.")] = None,
    fmt: Annotated[str | None, typer.Option("--format", help="json | md | html | all.")] = None,
    timeout: Annotated[float | None, typer.Option("--timeout", help="HTTP timeout (seconds).")] = None,
    reliability_requests: Annotated[int | None, typer.Option("--reliability-requests", help="Reliability probe request count (0 disables).")] = None,
    concurrency: Annotated[int | None, typer.Option("--concurrency", help="Reliability probe concurrency.")] = None,
    max_context_tokens: Annotated[int | None, typer.Option("--max-context-tokens", help="Cap for the real-context-window probe.")] = None,
    kb_dir: Annotated[list[Path] | None, typer.Option("--kb-dir", help="Extra knowledge-base directory (repeatable).")] = None,
    fail_under: Annotated[float | None, typer.Option("--fail-under", help="Exit 1 if overall score < this.")] = None,
    fail_on_risk: Annotated[str | None, typer.Option("--fail-on-risk", help="Exit 1 if risk >= this (low|medium|high).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full JSON report to stdout instead of writing files.")] = False,
    compact: Annotated[bool, typer.Option("--compact", help="Print a lean agent/LLM-facing JSON verdict to stdout (much smaller than --json).")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show which detectors would run and the estimated API-call count, without making any requests.")] = False,
) -> None:
    """Audit one relay endpoint and write a report."""
    try:
        cfg = load_config_file(config)
        target = _target_from(
            cfg, "target", kind="target", name=name, base_url=base_url, api_key=api_key,
            model=model, declared_provider=declared_provider, timeout=timeout, headers=header,
            api=api, claimed_model=claimed_model,
        )
        options = _build_options(
            cfg, suite=suite, judge=judge, only=only, skip=skip,
            reliability_requests=reliability_requests, concurrency=concurrency,
            max_context_tokens=max_context_tokens,
        )
        fail_on_risk = validate_risk(fail_on_risk)
        baseline = None
        judge_t = _judge_target(cfg, judge_base_url, judge_api_key, judge_model, baseline) if options.judge else None
        if dry_run:
            _emit_dry_run(
                _build_dry_run_plan(target, options, baseline, judge_t, "check"),
                machine=_machine_mode(as_json, compact),
            )
        _run_and_report(
            target, options, baseline=baseline, judge_target=judge_t, mode="check",
            out_dir=out_dir or Path(section(cfg, "run").get("output_dir") or "reports"),
            fmt=fmt or section(cfg, "run").get("format") or "all",
            fail_under=fail_under, fail_on_risk=fail_on_risk, as_json=as_json, as_compact=compact,
            kb_dirs=list(kb_dir) if kb_dir else None,
        )
    except ConfigError as exc:
        if _machine_mode(as_json, compact):
            _emit_machine_error(exc)
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


@app.command("compare")
def compare_command(
    config: Annotated[Path | None, typer.Option("--config", "-c", help="YAML config path.")] = None,
    target_base_url: Annotated[str | None, typer.Option("--target-base-url", help="Target relay base URL.")] = None,
    target_api_key: Annotated[str | None, typer.Option("--target-api-key", help="Target API key (env:VAR ok).")] = None,
    target_model: Annotated[str | None, typer.Option("--target-model", help="Target model id.")] = None,
    target_name: Annotated[str | None, typer.Option("--target-name", help="Target display name.")] = None,
    target_api: Annotated[str | None, typer.Option("--target-api", help="Target wire protocol: auto | openai | anthropic.")] = None,
    declared_provider: Annotated[str | None, typer.Option("--declared-provider", help="Provider hint for KB lookup.")] = None,
    baseline_base_url: Annotated[str | None, typer.Option("--baseline-base-url", help="Trusted baseline base URL.")] = None,
    baseline_api_key: Annotated[str | None, typer.Option("--baseline-api-key", help="Baseline API key (env:VAR ok).")] = None,
    baseline_model: Annotated[str | None, typer.Option("--baseline-model", help="Baseline model id.")] = None,
    baseline_name: Annotated[str | None, typer.Option("--baseline-name", help="Baseline display name.")] = None,
    baseline_api: Annotated[str | None, typer.Option("--baseline-api", help="Baseline wire protocol: auto | openai | anthropic.")] = None,
    suite: Annotated[str | None, typer.Option("--suite", help="smoke | standard | deep | full.")] = None,
    judge: Annotated[bool | None, typer.Option("--judge/--no-judge", help="Enable code+LLM hybrid judging.")] = None,
    judge_model: Annotated[str | None, typer.Option("--judge-model", help="Judge model id (defaults to baseline).")] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="Report output directory.")] = None,
    fmt: Annotated[str | None, typer.Option("--format", help="json | md | html | all.")] = None,
    timeout: Annotated[float | None, typer.Option("--timeout", help="HTTP timeout (seconds).")] = None,
    max_context_tokens: Annotated[int | None, typer.Option("--max-context-tokens", help="Cap for the context-window probe.")] = None,
    kb_dir: Annotated[list[Path] | None, typer.Option("--kb-dir", help="Extra knowledge-base directory (repeatable).")] = None,
    fail_under: Annotated[float | None, typer.Option("--fail-under", help="Exit 1 if overall score < this.")] = None,
    fail_on_risk: Annotated[str | None, typer.Option("--fail-on-risk", help="Exit 1 if risk >= this.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full JSON report to stdout.")] = False,
    compact: Annotated[bool, typer.Option("--compact", help="Print a lean agent/LLM-facing JSON verdict to stdout.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the detectors and estimated API calls without making requests.")] = False,
) -> None:
    """Audit a relay against a trusted baseline of the same declared model."""
    try:
        cfg = load_config_file(config)
        target = _target_from(
            cfg, "target", kind="target", name=target_name, base_url=target_base_url,
            api_key=target_api_key, model=target_model, declared_provider=declared_provider,
            timeout=timeout, headers=None, api=target_api,
        )
        baseline = _target_from(
            cfg, "baseline", kind="baseline", name=baseline_name, base_url=baseline_base_url,
            api_key=baseline_api_key, model=baseline_model, declared_provider=None,
            timeout=timeout, headers=None, api=baseline_api,
        )
        options = _build_options(cfg, suite=suite or "deep", judge=judge, max_context_tokens=max_context_tokens)
        fail_on_risk = validate_risk(fail_on_risk)
        judge_t = _judge_target(cfg, None, None, judge_model, baseline) if options.judge else None
        if dry_run:
            _emit_dry_run(
                _build_dry_run_plan(target, options, baseline, judge_t, "compare"),
                machine=_machine_mode(as_json, compact),
            )
        _run_and_report(
            target, options, baseline=baseline, judge_target=judge_t, mode="compare",
            out_dir=out_dir or Path(section(cfg, "run").get("output_dir") or "reports"),
            fmt=fmt or section(cfg, "run").get("format") or "all",
            fail_under=fail_under, fail_on_risk=fail_on_risk, as_json=as_json, as_compact=compact,
            kb_dirs=list(kb_dir) if kb_dir else None,
        )
    except ConfigError as exc:
        if _machine_mode(as_json, compact):
            _emit_machine_error(exc)
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc


@app.command("models")
def models_command(
    base_url: Annotated[str, typer.Option("--base-url", help="Endpoint base URL.")],
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key (env:VAR ok).")] = None,
    model: Annotated[str, typer.Option("--model", help="A model id (for the client; not required to list).")] = "x",
    api: Annotated[str | None, typer.Option("--api", help="Wire protocol: auto | openai | anthropic.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the model list as JSON.")] = False,
) -> None:
    """List the models an endpoint advertises via GET /v1/models."""
    try:
        target = build_target(
            kind="endpoint", name=None, base_url=base_url, api_key=api_key, model=model, api=api
        )
    except ConfigError as exc:
        if as_json:
            _emit_machine_error(exc)
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    async def _go() -> None:
        async with make_client(target) as client:
            outcome, ids = await client.list_models()
        if not outcome.ok:
            if as_json:
                print(json.dumps({
                    "ok": False, "base_url": base_url, "models": [],
                    "error": {"status_code": outcome.status_code, "message": outcome.error_message},
                }, ensure_ascii=False, indent=2))
                raise typer.Exit(code=1)
            err_console.print(f"[red]Failed:[/red] {outcome.error_message or outcome.status_code}")
            raise typer.Exit(code=1)
        if as_json:
            print(json.dumps({"ok": True, "base_url": base_url, "count": len(ids), "models": ids},
                             ensure_ascii=False, indent=2))
            return
        console.print(f"[green]{len(ids)} models[/green] at {base_url}")
        for mid in ids:
            console.print(f"  • {mid}")

    asyncio.run(_go())


@app.command("kb")
def kb_command(
    provider: Annotated[str | None, typer.Argument(help="Filter by provider key (openai, deepseek, ...).")] = None,
    kb_dir: Annotated[list[Path] | None, typer.Option("--kb-dir", help="Extra knowledge-base directory.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the knowledge base as JSON (for programmatic discovery).")] = False,
) -> None:
    """Inspect the bundled knowledge base."""
    kb = load_knowledge_base(list(kb_dir) if kb_dir else None)
    provs = [p for p in sorted(kb.providers.values(), key=lambda p: p.provider)
             if not provider or p.provider == provider]

    if as_json:
        models = [
            {
                "provider": prov.provider,
                "id": m.id,
                "aliases": m.aliases,
                "context_window_tokens": m.context_window_tokens,
                "max_output_tokens": m.max_output_tokens,
                "knowledge_cutoff": m.knowledge_cutoff,
                "tokenizer": m.tokenizer,
                "modalities": m.modalities,
                "reasoning": m.reasoning,
                "supports_tools": m.supports_tools,
                "supports_json_mode": m.supports_json_mode,
                "supports_json_schema": m.supports_json_schema,
            }
            for prov in provs
            for m in prov.models
        ]
        print(json.dumps({"count": len(models), "providers": [p.provider for p in provs], "models": models},
                         ensure_ascii=False, indent=2))
        return

    table = Table(title="zing knowledge base")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Context", justify="right")
    table.add_column("Max out", justify="right")
    table.add_column("Reasoning")
    for prov in provs:
        for m in prov.models:
            table.add_row(
                prov.provider,
                m.id,
                f"{m.context_window_tokens:,}" if m.context_window_tokens > 0 else "—",
                f"{m.max_output_tokens:,}" if m.max_output_tokens > 0 else "—",
                "yes" if m.reasoning else "",
            )
    console.print(table)
    total = sum(len(p.models) for p in provs)
    console.print(f"{total} models across {len(provs)} providers.")


@app.command("serve")
def serve_command(
    host: Annotated[str, typer.Option("--host", help="Bind address (default localhost only).")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to serve on.")] = 8000,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open the UI in a browser.")] = True,
) -> None:
    """Serve the local web UI — a point-and-click front end for `zing check`.

    Runs entirely on your machine: keys entered in the browser reach only this
    local server and the target relay, never a third party. Requires the web extra:
    `pip install 'zing-audit[web]'`.
    """
    try:
        import uvicorn

        from zing.web.server import create_app
    except ImportError as exc:
        err_console.print(
            "[red]The web UI needs the optional 'web' extra.[/red]\n"
            "Install it with:  [bold]pip install 'zing-audit[web]'[/bold]"
        )
        raise typer.Exit(code=2) from exc

    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"
    console.print(f"[green]zing[/green] web UI → [bold]{url}[/bold]   (Ctrl-C to stop)")
    if host == "0.0.0.0":
        err_console.print(
            "[yellow]![/yellow] Binding 0.0.0.0 exposes the audit API (and any keys you "
            "type) to your network. Prefer the default 127.0.0.1."
        )
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.command("watch")
def watch_command(
    config: Annotated[Path | None, typer.Option("--config", "-c", help="YAML config path (reuses the same [target]/[run] shape as `check`).")] = None,
    base_url: Annotated[str | None, typer.Option("--base-url", help="Relay base URL, e.g. https://relay.example.com/v1.")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key, or env:VAR / file:/path reference.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Model id actually sent in requests.")] = None,
    claimed_model: Annotated[str | None, typer.Option("--claimed-model", help="Model the relay claims to serve, if different from --model.")] = None,
    name: Annotated[str | None, typer.Option("--name", help="Display name for the target.")] = None,
    api: Annotated[str | None, typer.Option("--api", help="Wire protocol: auto | openai | anthropic.")] = None,
    declared_provider: Annotated[str | None, typer.Option("--declared-provider", help="Provider hint for KB lookup.")] = None,
    header: Annotated[list[str] | None, typer.Option("--header", "-H", help="Extra header 'Name: value' (repeatable).")] = None,
    suite: Annotated[str | None, typer.Option("--suite", help="smoke | standard | deep | full.")] = None,
    interval: Annotated[int, typer.Option("--interval", help="Seconds between re-audit cycles.")] = 3600,
    once: Annotated[bool, typer.Option("--once", help="Run a single cycle and exit (no loop).")] = False,
    webhook: Annotated[list[str] | None, typer.Option("--webhook", help="Alert webhook URL (repeatable).")] = None,
    webhook_kind: Annotated[str, typer.Option("--webhook-kind", help="auto | slack | feishu | dingtalk | generic.")] = "auto",
    alert_on: Annotated[str, typer.Option("--alert-on", help="Alert when risk >= this: low | medium | high.")] = "medium",
    alert_on_regression: Annotated[bool, typer.Option("--alert-on-regression/--no-alert-on-regression", help="Also alert when risk is worse than the previous saved run for this target+model.")] = True,
) -> None:
    """Continuously re-audit a relay on a schedule and alert webhooks on risk.

    Each cycle runs a `check`-mode audit, persists it to the local history store,
    compares against the previous saved run for this target+model, and POSTs a
    concise alert to each --webhook when the risk crosses --alert-on (or regresses,
    unless --no-alert-on-regression). Use --once for a single cycle (e.g. in cron),
    or leave it looping with --interval. Ctrl-C stops cleanly.
    """
    from zing import notify
    from zing.runner import run_audit

    try:
        cfg = load_config_file(config)
        target = _target_from(
            cfg, "target", kind="target", name=name, base_url=base_url, api_key=api_key,
            model=model, declared_provider=declared_provider, timeout=None, headers=header,
            api=api, claimed_model=claimed_model,
        )
        options = _build_options(cfg, suite=suite)
        # Validate --alert-on against the allowed risk levels (low|medium|high),
        # then map it to a RiskLevel for threshold comparison.
        validate_risk(alert_on)
        threshold = RiskLevel(alert_on)
    except ConfigError as exc:
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    webhooks = list(webhook or [])
    kb_dirs = None

    def _should_alert(report: AuditReport, previous: dict | None) -> bool:
        rank = _WATCH_RISK_RANK
        cur_rank = rank.get(report.verdict.risk_level, rank[RiskLevel.INCONCLUSIVE])
        if cur_rank >= rank[threshold]:
            return True
        if alert_on_regression and previous is not None:
            cur_dict = json.loads(report.model_dump_json())
            return notify.regressed(cur_dict, previous)
        return False

    def _previous_run(target: TargetConfig) -> dict | None:
        """The most recent prior saved report for this target+claimed model, if any."""
        try:
            from zing.web import history
        except Exception:
            return None
        try:
            rows = history.recent(limit=50)
        except Exception:
            return None
        claimed = target.claimed_model or target.model
        for row in rows:
            if row.get("base_url") == target.base_url and row.get("claimed_model") == claimed:
                rid = row.get("id")
                if rid is not None:
                    full = history.get(int(rid))
                    if full is not None:
                        return full
        return None

    async def _cycle() -> None:
        previous = _previous_run(target)
        command = "zing " + " ".join(sys.argv[1:])
        report = await run_audit(
            target, options, mode="check", command=command, kb_dirs=kb_dirs
        )

        # Persist (best-effort): a history failure must never break the watch loop.
        try:
            from zing.web import history
            history.save(json.loads(report.model_dump_json()))
        except Exception:
            pass

        v = report.verdict
        style, label = _RISK_STYLE.get(v.risk_level, ("white", v.risk_level.value))
        score = "n/a" if v.overall_score is None else f"{v.overall_score}/100"
        from datetime import datetime
        stamp = datetime.now().strftime("%H:%M:%S")
        console.print(
            f"[dim]{stamp}[/] [{style}]{label}[/] · {score} · "
            f"{target.base_url} · {target.claimed_model or target.model}"
        )

        if not _should_alert(report, previous):
            return
        if not webhooks:
            console.print("  [yellow]![/] alert condition met but no --webhook configured")
            return
        report_dict = json.loads(report.model_dump_json())
        for url in webhooks:
            ok = await notify.send(url, report_dict, kind=webhook_kind, previous=previous)
            mark = "[green]✓[/]" if ok else "[red]✗[/]"
            host = url.split("/")[2] if "://" in url else url
            console.print(f"  {mark} alert → {host}")

    async def _loop() -> None:
        while True:
            try:
                await _cycle()
            except Exception as exc:  # one bad cycle shouldn't kill the watcher
                err_console.print(f"[red]Cycle error:[/red] {exc}")
            if once:
                return
            await asyncio.sleep(max(1, interval))

    console.print(
        f"[green]zing watch[/] · every [bold]{interval}s[/] · "
        f"alert ≥ [bold]{threshold.value}[/]"
        + ("" if not webhooks else f" · {len(webhooks)} webhook(s)")
        + ("  (Ctrl-C to stop)" if not once else "")
    )
    try:
        asyncio.run(_loop())
    except KeyboardInterrupt:
        console.print("\n[dim]watch stopped.[/]")
        raise typer.Exit(code=0) from None


def _print_embed_verdict(verdict: dict, title: str) -> None:
    """Render an embedding/rerank verdict dict (from zing.embed_audit) with rich."""
    risk = RiskLevel(verdict["risk_level"])
    style, label = _RISK_STYLE.get(risk, ("white", risk.value))
    tgt = verdict["target"]
    body = (
        f"Target : {tgt['name']}  ·  model [bold]{tgt['model']}[/]"
        f"{'  ·  claimed ' + tgt['claimed_model'] if tgt.get('claimed_model') else ''}"
        f"{'  ·  provider ' + tgt['declared_provider'] if tgt.get('declared_provider') else ''}\n"
        f"Score  : {verdict['score']}/100"
    )
    console.print(Panel(body, title=f"[{style}]{label}[/]  —  {title}", border_style=style.split()[-1]))

    table = Table(show_lines=False, expand=False)
    table.add_column("Status")
    table.add_column("Severity")
    table.add_column("Check")
    table.add_column("Summary")
    for f in verdict["findings"]:
        st = Status(f["status"])
        st_style = _STATUS_STYLE.get(st, "white")
        table.add_row(
            f"[{st_style}]{st.value}[/]",
            f["severity"],
            f["id"],
            f["summary"],
        )
    console.print(table)


@app.command("embed")
def embed_command(
    base_url: Annotated[str | None, typer.Option("--base-url", help="Embedding endpoint base URL, e.g. https://relay.example.com/v1.")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key, or env:VAR / file:/path reference.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Embedding model id actually sent in requests.")] = None,
    claimed_model: Annotated[str | None, typer.Option("--claimed-model", help="Model the relay claims to serve, if different from --model (resolves the claimed dimension from the KB).")] = None,
    declared_provider: Annotated[str | None, typer.Option("--declared-provider", help="Provider hint for KB lookup (openai, qwen, ...).")] = None,
    claimed_dimensions: Annotated[int | None, typer.Option("--claimed-dimensions", help="Override the expected vector dimension instead of resolving it from the KB (0 = unknown).")] = None,
    kb_dir: Annotated[list[Path] | None, typer.Option("--kb-dir", help="Extra knowledge-base directory (repeatable).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the verdict as JSON to stdout.")] = False,
    fail_on_risk: Annotated[str | None, typer.Option("--fail-on-risk", help="Exit 1 if risk >= this (low|medium|high).")] = None,
) -> None:
    """Audit an embeddings endpoint (a non-chat surface).

    Runs embedding-specific checks — connectivity, vector-dimension match against
    the claimed model, determinism, and distinctness — instead of the chat
    detector pipeline. The expected dimension is resolved from the bundled
    knowledge base for the claimed model (override with --claimed-dimensions).
    """
    try:
        target = build_target(
            kind="target", name=None, base_url=base_url, api_key=api_key, model=model,
            declared_provider=declared_provider, claimed_model=claimed_model,
        )
        fail_on_risk = validate_risk(fail_on_risk)
    except ConfigError as exc:
        if as_json:
            _emit_machine_error(exc)
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    # Resolve the claimed embedding dimension from the KB unless overridden.
    dims = claimed_dimensions if claimed_dimensions is not None else 0
    if claimed_dimensions is None:
        kb = load_knowledge_base(list(kb_dir) if kb_dir else None)
        resolved = kb.resolve(target.claimed, provider_hint=target.declared_provider)
        if resolved is not None:
            dims = resolved.model.embedding_dimensions

    from zing.embed_audit import audit_embeddings

    verdict = asyncio.run(audit_embeddings(target, dims))

    if as_json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        _print_embed_verdict(verdict, "Embedding audit")

    raise typer.Exit(code=_embed_exit_code(verdict, fail_on_risk))


@app.command("rerank")
def rerank_command(
    base_url: Annotated[str | None, typer.Option("--base-url", help="Rerank endpoint base URL, e.g. https://relay.example.com/v1.")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key, or env:VAR / file:/path reference.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Rerank model id actually sent in requests.")] = None,
    declared_provider: Annotated[str | None, typer.Option("--declared-provider", help="Provider hint (for display).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the verdict as JSON to stdout.")] = False,
    fail_on_risk: Annotated[str | None, typer.Option("--fail-on-risk", help="Exit 1 if risk >= this (low|medium|high).")] = None,
) -> None:
    """Audit a rerank endpoint with a built-in known-answer probe (non-chat surface).

    Sends a query with one obviously-relevant document mixed among distractors; a
    genuine reranker must rank that document first. Prints pass/fail.
    """
    try:
        target = build_target(
            kind="target", name=None, base_url=base_url, api_key=api_key, model=model,
            declared_provider=declared_provider,
        )
        fail_on_risk = validate_risk(fail_on_risk)
    except ConfigError as exc:
        if as_json:
            _emit_machine_error(exc)
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    # Built-in known-answer probe: document 2 is the clear answer to the query.
    query = "What is the capital of France?"
    documents = [
        "Bananas are a good source of potassium and dietary fiber.",
        "The Great Wall of China is visible from low Earth orbit on a clear day.",
        "Paris is the capital and most populous city of France.",
        "Photosynthesis converts sunlight into chemical energy in plants.",
    ]
    expected_top_index = 2

    from zing.embed_audit import audit_rerank

    verdict = asyncio.run(audit_rerank(target, query, documents, expected_top_index))

    if as_json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        _print_embed_verdict(verdict, "Rerank audit (known-answer probe)")

    raise typer.Exit(code=_embed_exit_code(verdict, fail_on_risk))


def _embed_exit_code(verdict: dict, fail_on_risk: str | None) -> int:
    """Exit 1 when the embedding/rerank verdict risk >= the --fail-on-risk gate."""
    if not fail_on_risk:
        return 0
    try:
        threshold = RiskLevel(fail_on_risk)
        risk = RiskLevel(verdict["risk_level"])
    except ValueError:
        return 0
    if (
        threshold in _RISK_ORDER
        and risk in _RISK_ORDER
        and _RISK_ORDER.index(risk) >= _RISK_ORDER.index(threshold)
    ):
        return 1
    return 0


@app.command("image")
def image_command(
    base_url: Annotated[str | None, typer.Option("--base-url", help="Image endpoint base URL, e.g. https://relay.example.com/v1.")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key, or env:VAR / file:/path reference.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Image model id actually sent in requests.")] = None,
    claimed_model: Annotated[str | None, typer.Option("--claimed-model", help="Model the relay claims to serve, if different from --model (resolves the native sizes from the KB).")] = None,
    declared_provider: Annotated[str | None, typer.Option("--declared-provider", help="Provider hint for KB lookup (openai, qwen, ...).")] = None,
    size: Annotated[str, typer.Option("--size", help="Requested image size as WxH.")] = "1024x1024",
    save: Annotated[Path | None, typer.Option("--save", help="Write the first generated image to this path for inspection.")] = None,
    kb_dir: Annotated[list[Path] | None, typer.Option("--kb-dir", help="Extra knowledge-base directory (repeatable).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the verdict as JSON to stdout.")] = False,
    fail_on_risk: Annotated[str | None, typer.Option("--fail-on-risk", help="Exit 1 if risk >= this (low|medium|high).")] = None,
) -> None:
    """Audit an image-generation endpoint (POST /v1/images/generations, a non-chat surface).

    Generates two images, decodes their headers with pure stdlib (no Pillow), and
    checks connectivity, format, that the returned WxH matches the request (and the
    claimed model's native sizes from the KB — the headline 货不对板 signal), the
    image count, and distinctness across two different prompts.
    """
    try:
        target = build_target(
            kind="target", name=None, base_url=base_url, api_key=api_key, model=model,
            declared_provider=declared_provider, claimed_model=claimed_model,
        )
        fail_on_risk = validate_risk(fail_on_risk)
    except ConfigError as exc:
        if as_json:
            _emit_machine_error(exc)
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    # Resolve the claimed native sizes from the KB for the size-match check.
    kb = load_knowledge_base(list(kb_dir) if kb_dir else None)
    resolved = kb.resolve(target.claimed, provider_hint=target.declared_provider)
    claimed_sizes = list(resolved.model.image_sizes) if resolved is not None else []

    from zing.media_audit import audit_image

    async def _go() -> tuple[dict, bytes | None]:
        # Optionally fetch one image for --save using the same client/decoding path.
        sample: bytes | None = None
        verdict = await audit_image(target, size=size, claimed_sizes=claimed_sizes)
        if save is not None:
            async with make_client(target) as client:
                _outcome, images = await client.images_generate(
                    "A single sample image for inspection.", size, n=1
                )
            if images:
                sample = images[0]
        return verdict, sample

    verdict, sample = asyncio.run(_go())
    if save is not None and sample:
        save.write_bytes(sample)

    if as_json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        _print_embed_verdict(verdict, "Image generation audit")
        if save is not None and sample:
            console.print(f"\n[dim]Saved sample image → {save}[/]")

    raise typer.Exit(code=_embed_exit_code(verdict, fail_on_risk))


@app.command("audio")
def audio_command(
    base_url: Annotated[str | None, typer.Option("--base-url", help="Audio/TTS endpoint base URL, e.g. https://relay.example.com/v1.")] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", help="API key, or env:VAR / file:/path reference.")] = None,
    model: Annotated[str | None, typer.Option("--model", help="TTS model id actually sent in requests.")] = None,
    claimed_model: Annotated[str | None, typer.Option("--claimed-model", help="Model the relay claims to serve, if different from --model.")] = None,
    declared_provider: Annotated[str | None, typer.Option("--declared-provider", help="Provider hint for KB lookup (openai, qwen, ...).")] = None,
    voice: Annotated[str, typer.Option("--voice", help="Voice id to request.")] = "alloy",
    fmt: Annotated[str, typer.Option("--format", help="Requested audio format (wav|mp3|opus|flac).")] = "wav",
    text: Annotated[str, typer.Option("--text", help="Probe sentence to synthesize.")] = "The quick brown fox jumps over the lazy dog.",
    save: Annotated[Path | None, typer.Option("--save", help="Write a synthesized clip to this path for inspection.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the verdict as JSON to stdout.")] = False,
    fail_on_risk: Annotated[str | None, typer.Option("--fail-on-risk", help="Exit 1 if risk >= this (low|medium|high).")] = None,
) -> None:
    """Audit an audio/TTS endpoint (POST /v1/audio/speech, a non-chat surface).

    Synthesizes a short and a long input, checks connectivity, that the body is
    actually the requested audio format (WAV/MP3/OGG/FLAC magic, not HTML/JSON),
    that the clip is non-trivial and its length scales with the input (WAV duration
    is decoded with the stdlib wave module), and distinctness across inputs.
    """
    try:
        target = build_target(
            kind="target", name=None, base_url=base_url, api_key=api_key, model=model,
            declared_provider=declared_provider, claimed_model=claimed_model,
        )
        fail_on_risk = validate_risk(fail_on_risk)
    except ConfigError as exc:
        if as_json:
            _emit_machine_error(exc)
        err_console.print(f"[red]Config error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    from zing.media_audit import audit_audio

    async def _go() -> tuple[dict, bytes | None]:
        sample: bytes | None = None
        verdict = await audit_audio(target, voice=voice, fmt=fmt)
        if save is not None:
            async with make_client(target) as client:
                _outcome, audio = await client.audio_speech(text, voice, fmt)
            if audio:
                sample = audio
        return verdict, sample

    verdict, sample = asyncio.run(_go())
    if save is not None and sample:
        save.write_bytes(sample)

    if as_json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
    else:
        _print_embed_verdict(verdict, "Audio (TTS) generation audit")
        if save is not None and sample:
            console.print(f"\n[dim]Saved sample clip → {save}[/]")

    raise typer.Exit(code=_embed_exit_code(verdict, fail_on_risk))


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
) -> None:
    if version:
        console.print(f"zing {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


if __name__ == "__main__":
    app()
