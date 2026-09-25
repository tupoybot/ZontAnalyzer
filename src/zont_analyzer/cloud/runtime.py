"""Small bounded HTTP runtime for the M2 cloud candidate."""

from __future__ import annotations

import base64
import contextlib
import hmac
import json
import logging
import multiprocessing
import os
import re
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from zont_analyzer.cloud import auth

MAX_BODY_BYTES = 65_536
MAX_RESULT_BYTES = 1_048_576
MAX_SITE_BYTES = 16_777_216
MAX_INPUT_SECONDS = 5.0
MAX_LOGIN_BYTES = 4096
_HTML_SITE = re.compile(
    r"\A/(?:|index\.html|latest\.html|(?:daily|weekly|monthly|seasonal)/\d{4}-\d{2}-\d{2}\.html"
    r"|za/?|za/(?:index\.html|latest\.html|(?:daily|weekly|monthly|seasonal)/\d{4}-\d{2}-\d{2}\.html))\Z"
)
logger = logging.getLogger(__name__)


class JobTimeoutError(RuntimeError):
    """The child process exceeded the request's overall job budget."""


class JobFailureError(RuntimeError):
    """The child process returned a deliberately redacted failure."""


class JobValidationError(JobFailureError):
    """The submitted job payload did not satisfy its bounded DTO."""


@dataclass(frozen=True)
class RuntimeConfig:
    environment: Literal["dev", "pilot"]
    port: int
    job_timeout_seconds: float
    revision: str
    authorization: str
    xray_proxy_port: int = 1080
    report_timeout_seconds: float = 180

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        environment = os.environ.get("CLOUD_ENVIRONMENT")
        if environment not in {"dev", "pilot"}:
            raise ValueError("CLOUD_ENVIRONMENT must be dev or pilot")
        credentials = os.environ.pop("CLOUD_WEB_CREDENTIALS", None)
        if not credentials or "\n" in credentials or "\r" in credentials:
            raise ValueError("CLOUD_WEB_CREDENTIALS is required")
        try:
            port = int(os.environ.get("PORT", "8080"))
            timeout = float(os.environ.get("CLOUD_JOB_TIMEOUT_SECONDS", "15"))
            report_timeout = float(os.environ.get("CLOUD_REPORT_TIMEOUT_SECONDS", "180"))
        except ValueError as exc:
            raise ValueError("invalid cloud runtime numeric configuration") from exc
        if not 1 <= port <= 65535 or not 1 <= timeout <= 20 or not 1 <= report_timeout <= 180:
            raise ValueError("cloud runtime limits are outside their allowed range")
        revision = os.environ.get("CLOUD_REVISION", "unknown")
        if not revision or len(revision) > 128:
            raise ValueError("invalid CLOUD_REVISION")
        return cls(
            environment=cast(Literal["dev", "pilot"], environment),
            port=port,
            job_timeout_seconds=timeout,
            revision=revision,
            authorization="Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii"),
            report_timeout_seconds=report_timeout,
        )


@dataclass
class Counters:
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, outcome: str) -> None:
        with self._lock:
            setattr(self, outcome, getattr(self, outcome) + 1)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {"successes": self.successes, "failures": self.failures, "timeouts": self.timeouts}


def _child_entry(
    result_pipe: Any, callable_: Callable[[dict[str, Any]], dict[str, Any]], payload: dict[str, Any]
) -> None:
    try:
        encoded = json.dumps(callable_(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            result_pipe.send(("failure", "ResultTooLargeError"))
        else:
            result_pipe.send(("ok", encoded.decode("utf-8")))
    except (TypeError, ValueError) as exc:
        result_pipe.send(("validation", type(exc).__name__))
    except Exception as exc:  # noqa: BLE001 - boundary intentionally hides messages
        result_pipe.send(("failure", type(exc).__name__))
    finally:
        result_pipe.close()


def run_bounded(
    callable_: Callable[[dict[str, Any]], dict[str, Any]], payload: dict[str, Any], timeout_seconds: float,
    cancelled: threading.Event | None = None,
) -> dict[str, Any]:
    """Run one callable in a spawn child; no timed-out work remains alive."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_child_entry, args=(sender, callable_, payload))
    started = time.monotonic()
    process_started = False
    sender_closed = False
    try:
        process.start()
        process_started = True
        sender.close()
        sender_closed = True
        while True:
            if cancelled is not None and cancelled.is_set():
                raise JobTimeoutError()
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise JobTimeoutError()
            if receiver.poll(min(remaining, 0.05)):
                try:
                    status, value = receiver.recv()
                except EOFError as exc:
                    raise JobFailureError("ChildExitError") from exc
                break
            if process.exitcode is not None:
                raise JobFailureError("ChildExitError")
        remaining = timeout_seconds - (time.monotonic() - started)
        process.join(max(0.0, remaining))
        if process.is_alive():
            raise JobTimeoutError()
        if status == "validation":
            raise JobValidationError(str(value))
        if status != "ok":
            raise JobFailureError(str(value))
        try:
            result = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise JobFailureError("InvalidChildResult") from exc
        if not isinstance(result, dict):
            raise JobFailureError("InvalidChildResult")
        return result
    finally:
        if not sender_closed:
            sender.close()
        if process_started:
            if process.is_alive():
                process.terminate()
            process.join(1)
            if process.is_alive():  # pragma: no cover - defensive platform fallback
                process.kill()
                process.join(1)
            if not process.is_alive():
                process.close()
        else:
            with contextlib.suppress(ValueError):
                process.close()
        receiver.close()


def _dispatch_analytics(payload: dict[str, Any]) -> dict[str, Any]:
    from zont_analyzer.cloud import analytics

    return analytics.analyze(payload)


def _dispatch_integrations(payload: dict[str, Any]) -> dict[str, Any]:
    from zont_analyzer.cloud import integrations

    return integrations.check(payload)


def _dispatch_reports(payload: dict[str, Any]) -> dict[str, Any]:
    from zont_analyzer.cloud import report_jobs

    request = dict(payload)
    timeout = request.pop("_runtime_timeout_seconds")
    result = report_jobs.execute(request, timeout_seconds=timeout)
    if result.get("status") not in {"busy", "not_due", "import_in_progress"}:
        _mark_worker_success()
    return result


def _mark_worker_success() -> None:
    from datetime import UTC, datetime

    from zont_analyzer.runtime import build_runtime

    runtime = build_runtime(None, None)
    try:
        runtime.db.set_app_meta("cloud-worker-last-success", datetime.now(UTC).isoformat())
    finally:
        runtime.db.close()


def _dispatch_maintenance(payload: dict[str, Any]) -> dict[str, Any]:
    from zont_analyzer.cloud import user_jobs

    request = dict(payload)
    result = user_jobs.execute(request)
    _mark_worker_success()
    return result


def _dispatch_publication(payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.pop("_runtime_timeout_seconds", None)
    if payload:
        raise ValueError("publication payload must be empty")
    from zont_analyzer.application.publication import publish_reports
    from zont_analyzer.runtime import build_runtime

    runtime = build_runtime(None, None)
    try:
        return publish_reports(runtime, batch_size=8)
    finally:
        runtime.db.close()


def _cloud_application_runtime() -> Any:
    """Open YDB for a web request without running startup maintenance on GET."""
    from zont_analyzer.adapters.ydb.application import Database
    from zont_analyzer.adapters.ydb.database import YdbConfig
    from zont_analyzer.config import load_config
    from zont_analyzer.runtime import Runtime

    loaded = load_config(None, None)
    db = Database(YdbConfig.from_environment(namespace=loaded.config.storage.namespace))
    try:
        db.initialize()
        return Runtime(loaded, db)
    except BaseException:
        db.close()
        raise


DISPATCHERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "analytics": _dispatch_analytics,
    "integrations": _dispatch_integrations,
    "reports": _dispatch_reports,
    "maintenance": _dispatch_maintenance,
    "publication": _dispatch_publication,
}


class CloudServer(ThreadingHTTPServer):
    config: RuntimeConfig
    tunnel: Any
    counters: Counters
    active_job: threading.BoundedSemaphore

    def __init__(
        self, address: tuple[str, int], config: RuntimeConfig, tunnel: Any, telemetry: Any = None,
        runtime_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(address, CloudHandler)
        self.config = config
        self.tunnel = tunnel
        self.telemetry = telemetry
        self.boot_id = str(uuid.uuid4())
        self.counters = Counters()
        self.active_job = threading.BoundedSemaphore(1)
        self.stopping = threading.Event()
        self.runtime_factory = runtime_factory or _cloud_application_runtime

    def get_request(self) -> tuple[socket.socket, Any]:
        request, address = super().get_request()
        request.settimeout(min(MAX_INPUT_SECONDS, self.config.job_timeout_seconds))
        return request, address


class CloudHandler(BaseHTTPRequestHandler):
    server: CloudServer

    def setup(self) -> None:
        super().setup()
        self._input_lock = threading.Lock()
        self._input_done = False
        self._input_timer = threading.Timer(MAX_INPUT_SECONDS, self._close_input)
        self._input_timer.daemon = True
        self._input_timer.start()

    def finish(self) -> None:
        self._finish_input()
        super().finish()

    def _close_input(self) -> None:
        with self._input_lock:
            if self._input_done:
                return
            self._input_done = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)

    def _finish_input(self) -> None:
        with self._input_lock:
            self._input_done = True
            self._input_timer.cancel()

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def do_PUT(self) -> None:  # noqa: N802
        self._handle()

    def _handle(self) -> None:
        path = urlsplit(self.path).path
        if path == "/login" and self.command == "GET":
            self._finish_input()
            self._reply_bytes(200, auth.login_page(authenticated=self._authorized()), "text/html; charset=utf-8")
            return
        if path == "/login" and self.command == "POST":
            self._login()
            return
        if path == "/logout" and self.command == "POST":
            self._logout()
            return
        internal_maintenance = self.command == "POST" and self.path == "/internal/maintenance"
        if not internal_maintenance and not self._authorized():
            self._finish_input()
            if self.command == "GET" and _HTML_SITE.fullmatch(path):
                self._reply_bytes(401, auth.login_page(), "text/html; charset=utf-8")
            else:
                self._reply(401, {"error": "unauthorized"}, basic_challenge=True)
            return
        if self.command == "GET":
            self._finish_input()
        if self.command == "GET" and self.path == "/ready":
            self._finish_input()
            self._reply(200 if self.server.tunnel.ready() else 503, {"ready": self.server.tunnel.ready()})
            return
        if self.command == "GET" and self.path == "/diagnostics":
            self._finish_input()
            self._reply(200, {
                "environment": self.server.config.environment,
                "revision": self.server.config.revision,
                "boot_id": self.server.boot_id,
                "xray_ready": self.server.tunnel.ready(),
                "counters": self.server.counters.snapshot(),
            })
            return
        if path.startswith(("/api/", "/za/api/")):
            from zont_analyzer.cloud import web_api

            if not web_api.prepare_input(self):
                return
            runtime = None
            try:
                runtime = self.server.runtime_factory()
                if web_api.handle(self, runtime):
                    return
            except Exception as exc:  # noqa: BLE001 - keep private DB/provider errors out of responses
                error_type = type(exc).__name__
                logger.error(json.dumps(
                    {"level": "ERROR", "message": "cloud API failed", "event": "cloud_api",
                     "error_type": error_type}, separators=(",", ":"),
                ))
                self._finish_input()
                self._reply(502, {"error": "api_unavailable", "error_type": error_type})
                return
            finally:
                if runtime is not None:
                    runtime.db.close()
        if self.command == "GET" and not path.startswith("/jobs/"):
            from zont_analyzer.cloud import site

            runtime = None
            try:
                runtime = self.server.runtime_factory()
                status, body, content_type = site.serve(runtime, self.path)
                if len(body) > MAX_SITE_BYTES:
                    raise ValueError("site artifact exceeds limit")
                self._finish_input()
                self._reply_bytes(status, body, content_type)
            except Exception as exc:  # noqa: BLE001 - site errors are redacted
                error_type = type(exc).__name__
                logger.error(json.dumps(
                    {"level": "ERROR", "message": "cloud site failed", "event": "cloud_site",
                     "error_type": error_type}, separators=(",", ":"),
                ))
                self._finish_input()
                self._reply(502, {"error": "site_unavailable", "error_type": error_type})
            finally:
                if runtime is not None:
                    runtime.db.close()
            return
        endpoint = {
            "/jobs/analytics": "analytics",
            "/jobs/integrations": "integrations",
            "/jobs/reports": "reports",
            "/jobs/maintenance": "maintenance",
            "/jobs/publication": "publication",
            "/internal/maintenance": "maintenance",
        }.get(self.path)
        if self.command != "POST" or endpoint is None:
            self._finish_input()
            self._reply(404, {"error": "not_found"})
            return
        if not self.server.tunnel.ready():
            self._finish_input()
            self._reply(503, {"error": "xray_unavailable"})
            return
        if internal_maintenance:
            # Invoker IAM is the perimeter for this exact private timer path.
            # Timer messages do not select work or alter maintenance bounds.
            self._finish_input()
            payload: dict[str, Any] = {}
        else:
            parsed_payload = self._json_body()
            if parsed_payload is None:
                return
            payload = parsed_payload
        if not self.server.active_job.acquire(blocking=False):
            self._reply(409, {"error": "job_busy"})
            return
        job_id = str(uuid.uuid4())
        started = time.monotonic()
        response_status: int
        response: dict[str, Any]
        try:
            try:
                request_payload = dict(payload)
                if endpoint == "analytics":
                    request_payload["period_id"] = job_id
                if endpoint in {"reports", "maintenance", "publication"}:
                    request_payload["_runtime_timeout_seconds"] = self.server.config.report_timeout_seconds
                result = run_bounded(
                    DISPATCHERS[endpoint], request_payload,
                    (self.server.config.report_timeout_seconds if endpoint in {"reports", "maintenance", "publication"}
                     else self.server.config.job_timeout_seconds), self.server.stopping,
                )
            except JobTimeoutError:
                self.server.counters.record("timeouts")
                self._log_job(job_id, "timeout")
                response_status, response = 504, {"error": "job_timeout", "job_id": job_id}
                telemetry_success = False
            except JobValidationError as exc:
                self.server.counters.record("failures")
                self._log_job(job_id, "invalid", type(exc).__name__)
                response_status, response = 400, {
                    "error": "invalid_job", "job_id": job_id, "error_type": type(exc).__name__,
                }
                telemetry_success = False
            except Exception as exc:  # noqa: BLE001 - response/log deliberately excludes exception details
                self.server.counters.record("failures")
                self._log_job(job_id, "failed", type(exc).__name__)
                response_status, response = 502, {
                    "error": "job_failed", "job_id": job_id, "error_type": type(exc).__name__,
                }
                telemetry_success = False
            else:
                self.server.counters.record("successes")
                self._log_job(job_id, "ok")
                response_status, response = 200, {"job_id": job_id, "result": result}
                telemetry_success = True
            self._send_telemetry(telemetry_success, time.monotonic() - started)
            self._reply(response_status, response)
        finally:
            self.server.active_job.release()

    def _login(self) -> None:
        if not auth.same_origin(self.headers, os.environ.get("CLOUD_PUBLIC_ORIGIN")):
            self._finish_input()
            self._reply_bytes(401, auth.login_page(failed=True), "text/html; charset=utf-8")
            return
        if self.headers.get("Transfer-Encoding"):
            self._finish_input()
            self._reply(400, {"error": "transfer_encoding_denied"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            self._finish_input()
            self._reply(415, {"error": "form_content_type_required"})
            return
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            size = -1
        if not 1 <= size <= MAX_LOGIN_BYTES:
            self._finish_input()
            self._reply(413, {"error": "invalid_body_size"})
            return
        try:
            raw = self.rfile.read(size)
        except (OSError, TimeoutError):
            self._finish_input()
            self._reply(408, {"error": "input_timeout"})
            return
        if len(raw) != size:
            self._finish_input()
            self._reply(400, {"error": "incomplete_body"})
            return
        self._finish_input()
        credentials = auth.parse_credentials(raw)
        if credentials is None or not auth.credentials_match(*credentials, self.server.config.authorization):
            self._reply_bytes(401, auth.login_page(failed=True), "text/html; charset=utf-8")
            return
        cookie = auth.session_cookie(auth.issue_session(self.server.config.authorization))
        self._reply_bytes(303, b"", "text/plain; charset=utf-8",
                          extra_headers={"Location": "/", "Set-Cookie": cookie})

    def _logout(self) -> None:
        if not auth.same_origin(self.headers, os.environ.get("CLOUD_PUBLIC_ORIGIN")):
            self._finish_input()
            self._reply(403, {"error": "origin_denied"})
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length", "0") != "0":
            self._finish_input()
            self._reply(400, {"error": "invalid_body_size"})
            return
        self._finish_input()
        self._reply_bytes(303, b"", "text/plain; charset=utf-8",
                          extra_headers={"Location": "/login", "Set-Cookie": auth.clear_cookie()})

    def _authorized(self) -> bool:
        return (hmac.compare_digest(self.headers.get("Authorization", ""), self.server.config.authorization)
                or auth.session_from_headers(self.headers, self.server.config.authorization))

    def _json_body(self) -> dict[str, Any] | None:
        if self.headers.get("Transfer-Encoding"):
            self._finish_input()
            self._reply(400, {"error": "transfer_encoding_denied"})
            return None
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._finish_input()
            self._reply(415, {"error": "json_content_type_required"})
            return None
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            size = -1
        if not 0 <= size <= MAX_BODY_BYTES:
            self._finish_input()
            self._reply(413, {"error": "invalid_body_size"})
            return None
        try:
            raw = self.rfile.read(size)
        except (OSError, TimeoutError):
            self._finish_input()
            self._reply(408, {"error": "input_timeout"})
            return None
        if len(raw) != size:
            self._finish_input()
            self._reply(400, {"error": "incomplete_body"})
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._finish_input()
            self._reply(400, {"error": "invalid_json"})
            return None
        if not isinstance(value, dict):
            self._finish_input()
            self._reply(400, {"error": "json_object_required"})
            return None
        self._finish_input()
        return value

    def _reply(self, status: int, payload: dict[str, Any], *, basic_challenge: bool = False) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._reply_bytes(status, encoded, "application/json; charset=utf-8",
                          basic_challenge=basic_challenge)

    def _reply_bytes(self, status: int, encoded: bytes, content_type: str, *,
                     basic_challenge: bool = False, extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if basic_challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Zont cloud runtime"')
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(encoded)
        self.close_connection = True

    def _log_job(self, job_id: str, status: str, error_type: str | None = None) -> None:
        payload: dict[str, str] = {
            "level": "INFO" if status == "ok" else "ERROR",
            "message": "cloud job",
            "event": "cloud_job",
            "job_id": job_id,
            "status": status,
        }
        if error_type is not None:
            payload["error_type"] = error_type
        logger.info(json.dumps(payload, separators=(",", ":")))

    def _send_telemetry(self, success: bool, duration: float) -> None:
        if self.server.telemetry is None:
            return
        try:
            self.server.telemetry.send(success, duration)
        except Exception as exc:  # noqa: BLE001 - telemetry cannot alter a completed job response
            logger.info(json.dumps(
                {
                    "level": "ERROR",
                    "message": "telemetry export failed",
                    "event": "telemetry",
                    "status": "failed",
                    "error_type": type(exc).__name__,
                },
                separators=(",", ":"),
            ))

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def main() -> None:
    """Start the HTTP server after Xray has become ready on loopback."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = RuntimeConfig.from_environment()
    raw_xray = os.environ.pop("XRAY_CONFIG", None)
    if not raw_xray:
        raise RuntimeError("XRAY_CONFIG is required")
    from zont_analyzer.cloud.egress import Tunnel

    tunnel = None
    server = None
    try:
        tunnel = Tunnel(raw_xray, config.xray_proxy_port)
        telemetry_raw = os.environ.pop("GRAFANA_OTLP_CONFIG", "")
        telemetry = None
        if telemetry_raw:
            from zont_analyzer.cloud.telemetry import Telemetry

            telemetry = Telemetry(telemetry_raw, config.environment)
        server = CloudServer(("0.0.0.0", config.port), config, tunnel, telemetry)
        def stop_server(*_args: Any) -> None:
            server.stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        for received_signal in (signal.SIGINT, signal.SIGTERM):
            signal.signal(received_signal, stop_server)
        server.serve_forever()
    finally:
        if server is not None:
            server.server_close()
        if tunnel is not None:
            tunnel.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - startup values can contain secret configuration
        logger.error(json.dumps(
            {"level": "ERROR", "message": "cloud runtime startup failed", "event": "cloud_runtime_startup"},
            separators=(",", ":"),
        ))
        raise SystemExit(1) from None
