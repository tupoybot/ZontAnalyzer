from __future__ import annotations

import json
import logging
import re
import signal
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from zont_analyzer.application.feedback import start_feedback_server
from zont_analyzer.application.pilot import PilotService, worker_health, worker_status_path
from zont_analyzer.application.regeneration import run_sync
from zont_analyzer.config import explain_config
from zont_analyzer.doctor import run_doctor
from zont_analyzer.logging import configure_logging
from zont_analyzer.reports import render_html, render_text
from zont_analyzer.reports.chart_data import cached_chart_data
from zont_analyzer.runtime import Runtime, build_runtime

app = typer.Typer(help="Read-only ZONT telemetry collector and heating analyst.", no_args_is_help=True)
analyze_app = typer.Typer(help="Run deterministic analysis.")
report_app = typer.Typer(help="Show and export reports.")
recommendations_app = typer.Typer(help="Manage recommendation lifecycle.")
config_app = typer.Typer(help="Inspect effective configuration.")
notifications_app = typer.Typer(help="Test notification delivery.")
db_app = typer.Typer(help="Database maintenance.")
app.add_typer(analyze_app, name="analyze")
app.add_typer(report_app, name="report")
app.add_typer(recommendations_app, name="recommendations")
app.add_typer(config_app, name="config")
app.add_typer(notifications_app, name="notifications")
app.add_typer(db_app, name="db")


class State:
    runtime: Runtime


def _json(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.callback()
def callback(
    ctx: typer.Context,
    config: Annotated[Path | None, typer.Option("--config", help="Path to config.yaml")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir", help="Persistent data directory")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    configure_logging(verbose)
    try:
        state = State()
        state.runtime = build_runtime(config, data_dir)
        ctx.obj = state
    except Exception as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(2) from exc


def _runtime(ctx: typer.Context) -> Runtime:
    return ctx.ensure_object(State).runtime


@app.command("init")
def initialize(ctx: typer.Context) -> None:
    runtime = _runtime(ctx)
    runtime.loaded.data_dir.mkdir(parents=True, exist_ok=True)
    _json(
        {
            "initialized": True,
            "data_dir": runtime.loaded.data_dir,
            "db": runtime.db.path,
            "schema_revision": runtime.migration.revision,
            "migrated_from": runtime.migration.previous_revision,
            "pre_migration_backup": runtime.migration.backup_path,
        }
    )


@app.command()
def doctor(ctx: typer.Context, live: Annotated[bool, typer.Option("--live")] = False) -> None:
    result = run_doctor(_runtime(ctx), live=live)
    _json(result)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command()
def status(ctx: typer.Context) -> None:
    _json(_runtime(ctx).db.status())


@app.command()
def healthcheck(
    ctx: typer.Context,
    max_age_seconds: Annotated[
        int | None,
        typer.Option("--max-age-seconds", min=1, help="Maximum acceptable worker status age"),
    ] = None,
) -> None:
    runtime = _runtime(ctx)
    maximum = max_age_seconds or max(runtime.config.scheduler.sync_every_minutes * 180, 300)
    result = worker_health(worker_status_path(runtime), max_age_seconds=maximum)
    _json(result)
    if not result["ok"]:
        raise typer.Exit(1)


@app.command()
def discover(ctx: typer.Context) -> None:
    runtime = _runtime(ctx)
    with runtime.zont_client() as client:
        result = runtime.ingestion(client).discover()
    _json(result)


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([dhm])", value)
    if not match:
        raise typer.BadParameter("Use a duration such as 90d, 24h, or 30m")
    amount = int(match.group(1))
    return {"d": timedelta(days=amount), "h": timedelta(hours=amount), "m": timedelta(minutes=amount)}[match.group(2)]


@app.command()
def sync(
    ctx: typer.Context,
    backfill: Annotated[str | None, typer.Option("--backfill", help="History window, e.g. 90d")] = None,
) -> None:
    runtime = _runtime(ctx)
    with runtime.zont_client() as client:
        result = runtime.ingestion(client).sync(backfill=_duration(backfill) if backfill else None)
    _json(result)
    if not result.get("complete", False):
        raise typer.Exit(1)


@analyze_app.command("initial")
def analyze_initial(
    ctx: typer.Context,
    no_ai: Annotated[bool, typer.Option("--no-ai")] = False,
    days: Annotated[
        int | None, typer.Option("--days", min=1, max=365, help="Optional window; default all history")
    ] = None,
) -> None:
    report = _runtime(ctx).analysis(no_ai=no_ai).analyze_initial(use_ai=not no_ai, days=days)
    typer.echo(render_text(report, current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c))


@recommendations_app.command("maintain")
def recommendations_maintain(ctx: typer.Context) -> None:
    _json(_runtime(ctx).db.expire_stale_recommendations())


@analyze_app.command("daily")
def analyze_daily(
    ctx: typer.Context,
    selected: Annotated[str | None, typer.Option("--date", help="Local date YYYY-MM-DD")] = None,
    no_ai: Annotated[bool, typer.Option("--no-ai")] = False,
) -> None:
    try:
        analysis = _runtime(ctx).analysis(no_ai=no_ai)
        report_date = date.fromisoformat(selected) if selected else analysis.local_today() - timedelta(days=1)
    except ValueError as exc:
        raise typer.BadParameter("Date must look like 2026-08-01") from exc
    report = analysis.analyze_daily(report_date, use_ai=not no_ai)
    typer.echo(render_text(report, current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c))


@analyze_app.command("weekly")
def analyze_weekly(
    ctx: typer.Context,
    week: Annotated[str, typer.Option("--week", help="ISO week YYYY-Www")],
    no_ai: Annotated[bool, typer.Option("--no-ai")] = False,
) -> None:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", week)
    if not match:
        raise typer.BadParameter("Week must look like 2026-W31")
    report = (
        _runtime(ctx).analysis(no_ai=no_ai).analyze_week(int(match.group(1)), int(match.group(2)), use_ai=not no_ai)
    )
    typer.echo(render_text(report, current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c))


@analyze_app.command("monthly")
def analyze_monthly(
    ctx: typer.Context,
    month: Annotated[str, typer.Option("--month", help="Month YYYY-MM")],
    no_ai: Annotated[bool, typer.Option("--no-ai")] = False,
) -> None:
    match = re.fullmatch(r"(\d{4})-(\d{2})", month)
    if not match:
        raise typer.BadParameter("Month must look like 2026-07")
    report = (
        _runtime(ctx).analysis(no_ai=no_ai).analyze_month(int(match.group(1)), int(match.group(2)), use_ai=not no_ai)
    )
    typer.echo(render_text(report, current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c))


@analyze_app.command("seasonal")
def analyze_seasonal(
    ctx: typer.Context,
    season: Annotated[str, typer.Option("--season", help="YYYY-winter")],
    no_ai: Annotated[bool, typer.Option("--no-ai")] = False,
) -> None:
    match = re.fullmatch(r"(\d{4})-(winter|spring|summer|autumn)", season)
    if not match:
        raise typer.BadParameter("Season must look like 2026-winter")
    report = _runtime(ctx).analysis(no_ai=no_ai).analyze_season(int(match.group(1)), match.group(2), use_ai=not no_ai)
    typer.echo(render_text(report, current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c))


@report_app.command("latest")
def report_latest(ctx: typer.Context) -> None:
    report = _runtime(ctx).db.latest_report()
    if report is None:
        raise typer.BadParameter("No reports exist")
    typer.echo(render_text(report, current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c))


@report_app.command("publish")
def report_publish(ctx: typer.Context) -> None:
    """Publish existing completed daily/weekly/monthly reports without analysis or AI."""
    from zont_analyzer.application.publication import publish_reports

    _json(publish_reports(_runtime(ctx)))


@report_app.command("show")
def report_show(ctx: typer.Context, report_id: str) -> None:
    report = _runtime(ctx).db.report(report_id)
    if report is None:
        raise typer.BadParameter(f"Unknown report: {report_id}")
    _json(report.model_dump(mode="json"))


@report_app.command("regenerate")
def report_regenerate(
    ctx: typer.Context,
    report_id: str,
    question: Annotated[
        str | None,
        typer.Option("--question", help="Optional counterfactual question (up to 500 characters)"),
    ] = None,
) -> None:
    """Regenerate one report in the foreground, optionally answering an owner question."""
    try:
        result = run_sync(_runtime(ctx), report_id, question)
    except KeyError as exc:
        raise typer.BadParameter(f"Unknown report: {report_id}") from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _json(result)


@report_app.command("export")
def report_export(
    ctx: typer.Context,
    report_id: Annotated[str | None, typer.Argument(help="Report ID; omit to export latest")] = None,
    format_: Annotated[str, typer.Option("--format")] = "html",
    output: Annotated[Path | None, typer.Option("--output", "-o")] = None,
) -> None:
    db = _runtime(ctx).db
    report = db.report(report_id) if report_id else db.latest_report()
    if report is None:
        raise typer.BadParameter(f"Unknown report: {report_id}" if report_id else "No reports exist")
    from zont_analyzer.application.gas import GasService

    report = GasService(db, _runtime(ctx).config).refresh_cost(report)
    if format_ == "html":
        content = render_html(
            report,
            db.recommendation_views_for_report(report.id),
            chart_data=cached_chart_data(db, report),
            feedback_api_base_url=_runtime(ctx).config.feedback.public_api_base_url,
            current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c,
        )
    elif format_ in {"text", "md"}:
        content = render_text(report, current_comfort_band_c=_runtime(ctx).config.preferences.comfort_band_c)
    elif format_ == "json":
        content = report.model_dump_json(indent=2)
    else:
        raise typer.BadParameter("Format must be html, text, md, or json")
    output = output or Path(f"{report.id.replace(':', '-')}.{format_}")
    output.write_text(content, encoding="utf-8")
    typer.echo(str(output.resolve()))


@recommendations_app.command("list")
def recommendations_list(ctx: typer.Context) -> None:
    _json(_runtime(ctx).db.recommendations())


@recommendations_app.command("show")
def recommendations_show(ctx: typer.Context, recommendation_id: str) -> None:
    value = _runtime(ctx).db.recommendation(recommendation_id)
    if value is None:
        raise typer.BadParameter(f"Unknown recommendation: {recommendation_id}")
    _json(value)


@recommendations_app.command("mark-applied")
def recommendations_mark_applied(
    ctx: typer.Context,
    recommendation_id: str,
    note: Annotated[str, typer.Option("--note")],
) -> None:
    _json({"intervention_id": _runtime(ctx).db.mark_applied(recommendation_id, note)})


@recommendations_app.command("reject")
def recommendations_reject(
    ctx: typer.Context,
    recommendation_id: str,
    reason: Annotated[str, typer.Option("--reason")],
) -> None:
    _runtime(ctx).db.reject(recommendation_id, reason)
    _json({"rejected": recommendation_id})


@config_app.command("explain")
def config_explain(ctx: typer.Context) -> None:
    _json(explain_config(_runtime(ctx).loaded))


@config_app.command("export-inferred")
def config_export_inferred(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option("--output", "-o")] = Path("inferred-profile.json"),
) -> None:
    runtime = _runtime(ctx)
    payload = {"devices": runtime.db.list_devices(), "series": runtime.db.list_series()}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    typer.echo(str(output.resolve()))


@notifications_app.command("test")
def notifications_test(ctx: typer.Context) -> None:
    delivered = _runtime(ctx).db.flush_log_outbox()
    for message in delivered:
        logging.getLogger("zont_analyzer.notifications").info(message)
    _json({"delivered": len(delivered), "channel": "log"})


@db_app.command("backup")
def db_backup(ctx: typer.Context) -> None:
    runtime = _runtime(ctx)
    destination = Path(runtime.config.storage.backup_dir)
    if not destination.is_absolute():
        destination = runtime.loaded.data_dir / destination
    typer.echo(str(runtime.db.backup(destination)))


@app.command()
def run(ctx: typer.Context, once: Annotated[bool, typer.Option("--once")] = False) -> None:
    runtime = _runtime(ctx)
    worker = PilotService(runtime)
    stopping = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    feedback_server = None
    feedback_thread = None
    if not once and runtime.config.feedback.enabled:
        feedback_server, feedback_thread = start_feedback_server(runtime)
        logging.getLogger("zont_analyzer.feedback").info(
            "feedback API listening on %s:%d",
            runtime.config.feedback.listen_host,
            runtime.config.feedback.listen_port,
        )
    try:
        while not stopping.is_set():
            try:
                result = worker.run_cycle()
                logging.getLogger("zont_analyzer.worker").info("worker cycle complete: %s", result)
                if once:
                    _json(result)
            except Exception as exc:
                logging.getLogger("zont_analyzer.worker").exception("worker iteration failed")
                if once:
                    typer.echo(f"Worker cycle failed: {type(exc).__name__}: {exc}", err=True)
                    raise typer.Exit(1) from exc
            if once:
                break
            stopping.wait(runtime.config.scheduler.sync_every_minutes * 60)
    finally:
        if feedback_server is not None:
            feedback_server.shutdown()
            feedback_server.server_close()
        if feedback_thread is not None:
            feedback_thread.join(timeout=5)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
