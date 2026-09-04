from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from zont_analyzer.application.pilot import atomic_write_text, reports_directory
from zont_analyzer.reports import render_html
from zont_analyzer.runtime import Runtime

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 16_384
MAX_OWNER_NOTE_LENGTH = 2_000


def _api_path(runtime: Runtime) -> str:
    path = urlsplit(runtime.config.feedback.public_api_base_url).path.rstrip("/")
    if not path.startswith("/"):
        raise ValueError("feedback.public_api_base_url must contain an absolute URL path")
    return path


def _public_feedback(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "recommendation_id": value["id"],
        "report_id": value["report_id"],
        "status": value["status"],
        "owner_note": value.get("owner_note") or "",
        "updated_at": value["updated_at"],
    }


def publish_feedback_report(runtime: Runtime, report_id: str) -> None:
    """Refresh public HTML artifacts after a lifecycle write."""
    report = runtime.db.report(report_id)
    if report is None:
        return
    output_dir = reports_directory(runtime)
    rendered = render_html(
        report,
        runtime.db.recommendation_views_for_report(report.id),
        feedback_api_base_url=runtime.config.feedback.public_api_base_url,
    )
    if report.kind == "daily":
        local_date = report.period_start.astimezone(ZoneInfo(report.timezone)).date()
        archive = output_dir / "daily" / f"{local_date.isoformat()}.html"
        if archive.exists():
            atomic_write_text(archive, rendered, mode=0o644)
    latest = runtime.db.latest_report()
    latest_path = output_dir / "latest.html"
    if latest is not None and latest.id == report.id and latest_path.exists():
        atomic_write_text(latest_path, rendered, mode=0o644)


class FeedbackHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_feedback_server(runtime: Runtime) -> FeedbackHttpServer:
    api_path = _api_path(runtime)
    route_prefix = f"{api_path}/recommendations/"

    class Handler(BaseHTTPRequestHandler):
        server_version = "ZontAnalyzerFeedback/1"

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def _recommendation_id(self) -> str | None:
            path = urlsplit(self.path).path
            suffix = "/feedback"
            if not path.startswith(route_prefix) or not path.endswith(suffix):
                return None
            encoded_id = path[len(route_prefix) : -len(suffix)]
            if not encoded_id or "/" in encoded_id:
                return None
            return unquote(encoded_id)

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path == f"{api_path}/health":
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            recommendation_id = self._recommendation_id()
            if recommendation_id is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Маршрут не найден."})
                return
            value = runtime.db.recommendation(recommendation_id)
            if value is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Рекомендация не найдена."})
                return
            self._send_json(HTTPStatus.OK, _public_feedback(value))

        def do_PUT(self) -> None:  # noqa: N802
            recommendation_id = self._recommendation_id()
            if recommendation_id is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Маршрут не найден."})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Некорректная длина запроса."})
                return
            if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Некорректный размер запроса."})
                return
            try:
                payload = json.loads(self.rfile.read(content_length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Тело запроса должно быть JSON."})
                return
            if not isinstance(payload, dict) or set(payload) - {"status", "owner_note"}:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Допустимы только status и owner_note."})
                return
            status = payload.get("status")
            owner_note = payload.get("owner_note", "")
            if status not in {"applied", "rejected"}:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Некорректный статус."})
                return
            if owner_note is not None and not isinstance(owner_note, str):
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Комментарий должен быть строкой."})
                return
            if len(owner_note or "") > MAX_OWNER_NOTE_LENGTH:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": "Комментарий слишком длинный."})
                return
            try:
                value = runtime.db.set_recommendation_feedback(recommendation_id, status, owner_note)
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Рекомендация не найдена."})
                return
            response = _public_feedback(value)
            try:
                publish_feedback_report(runtime, str(value["report_id"]))
            except (OSError, ValueError) as exc:
                logger.warning("Feedback saved but report republish failed: %s", type(exc).__name__)
                response["publish_warning"] = "Обратная связь сохранена, HTML обновится в следующем цикле."
            self._send_json(HTTPStatus.OK, response)

        def do_POST(self) -> None:  # noqa: N802
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Используйте PUT."})

        def log_message(self, format_: str, *args: Any) -> None:
            logger.info("feedback http: " + format_, *args)

    return FeedbackHttpServer(
        (runtime.config.feedback.listen_host, runtime.config.feedback.listen_port),
        Handler,
    )


def start_feedback_server(runtime: Runtime) -> tuple[FeedbackHttpServer, threading.Thread]:
    server = build_feedback_server(runtime)
    thread = threading.Thread(target=server.serve_forever, name="feedback-http", daemon=True)
    thread.start()
    return server, thread
