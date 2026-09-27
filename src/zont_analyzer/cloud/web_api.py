"""Direct adapter for the application's authenticated HTTP routes."""

from __future__ import annotations

import io
import os
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import httpx

from zont_analyzer.adapters.openai.model_catalog import OpenAIModelCatalog
from zont_analyzer.application.ai_maintenance import local_assessments
from zont_analyzer.application.feedback import MAX_REQUEST_BYTES, feedback_handler_type
from zont_analyzer.application.model_review import ModelReviewStore
from zont_analyzer.cloud.egress import ReportTransport
from zont_analyzer.runtime import Runtime


def _origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (
            443 if parsed.scheme == "https" else 80
        )
    except ValueError:
        return None


def _same_origin(headers: Any, expected_origin: str | None) -> bool:
    if headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    origin = headers.get("Origin")
    if origin is None:
        return True
    expected = expected_origin or "https://" + headers.get("Host", "")
    return _origin(origin) is not None and _origin(origin) == _origin(expected)


def prepare_input(handler: Any) -> bool:
    """Finish bounded API request intake before opening YDB or Object Storage."""
    if handler.headers.get("Transfer-Encoding"):
        handler._finish_input()
        handler._reply(400, {"error": "transfer_encoding_denied"})
        return False
    if handler.command not in {"PUT", "POST"}:
        handler._finish_input()
        return True
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        length = -1
    if length < 0 or length > MAX_REQUEST_BYTES:
        handler._finish_input()
        handler._reply(413, {"error": "invalid_body_size"})
        return False
    try:
        raw = handler.rfile.read(length)
    except (OSError, TimeoutError):
        handler._finish_input()
        handler._reply(408, {"error": "input_timeout"})
        return False
    if len(raw) != length:
        handler._finish_input()
        handler._reply(400, {"error": "incomplete_body"})
        return False
    handler._api_body = raw
    handler._finish_input()
    return True

def handle(handler: Any, runtime: Runtime) -> bool:
    """Run existing routes against the live request object, without HTTP proxying."""
    api_path = urlsplit(runtime.config.feedback.public_api_base_url).path.rstrip("/") or "/api"
    requested_path = urlsplit(handler.path).path
    legacy_prefix = "/za" + api_path
    if requested_path.startswith(legacy_prefix + "/"):
        normalized_path = handler.path[3:]
    elif requested_path.startswith(api_path + "/"):
        normalized_path = handler.path
    else:
        return False
    if handler.command not in {"GET", "PUT", "POST"}:
        return False
    expected_origin = os.environ.get("CLOUD_PUBLIC_ORIGIN")
    if expected_origin and _origin(expected_origin) is None:
        raise ValueError("invalid CLOUD_PUBLIC_ORIGIN")

    shared_type = feedback_handler_type(runtime)

    class CloudRoutes(shared_type):  # type: ignore[valid-type,misc]
        def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            handler._finish_input()
            handler._reply(int(status), payload)

        def _same_origin_write(self) -> bool:
            return _same_origin(self.headers, expected_origin)

        def _publish_feedback(self, _report_id: str) -> None:
            # The YDB feedback write marks the affected report dirty.
            return

        def _publish_profile(self) -> None:
            # Owner-profile revision is written transactionally with the profile.
            return

        def _publish_tariffs(self, _start: Any, _end: Any) -> None:
            # Tariff revision is written transactionally with the tariff.
            return

        def _start_regeneration(self, report_id: str, question: str | None) -> dict[str, Any]:
            from zont_analyzer.cloud.user_jobs import enqueue_regeneration

            return enqueue_regeneration(runtime, report_id, question)

        def _regeneration_status(self, report_id: str) -> dict[str, Any]:
            from zont_analyzer.cloud.user_jobs import regeneration_status

            return regeneration_status(runtime, report_id)

        def _decide_model_review(self, proposal_id: str, action: str,
                                 expected_version: int, settings: Any) -> None:
            # Proposal acceptance rechecks official docs through the approved CONNECT route.
            with httpx.Client(transport=ReportTransport(), trust_env=False,
                              follow_redirects=False, timeout=8.0) as client:
                catalog = OpenAIModelCatalog(client=client)
                ModelReviewStore(runtime.db, catalog, assessments=local_assessments(runtime)).decide(
                    proposal_id, action, expected_version, settings,
                )

        def _start_review(self) -> None:
            from zont_analyzer.cloud.user_jobs import enqueue_review

            enqueue_review(runtime)

        def _review_state(self) -> dict[str, Any]:
            from zont_analyzer.application.ai_maintenance import review_state
            from zont_analyzer.cloud.user_jobs import review_status

            result = review_state(runtime)
            queued = review_status(runtime)
            result["running"] = result["running"] or queued.get("status") in {"queued", "running"}
            result["job_status"] = queued.get("status", "idle")
            return result

        def _worker_health(self) -> dict[str, Any]:
            raw = runtime.db.get_app_meta("cloud-worker-last-success")
            if not raw:
                return {"ok": False}
            try:
                last = datetime.fromisoformat(raw)
                age = (datetime.now(UTC) - last).total_seconds()
                fresh = last.tzinfo is not None and 0 <= age <= max(
                    runtime.config.scheduler.sync_every_minutes * 180, 300
                )
            except (TypeError, ValueError):
                fresh = False
            return {"ok": fresh}

    delegate = object.__new__(CloudRoutes)
    delegate.path = normalized_path
    delegate.command = handler.command
    delegate.headers = handler.headers
    delegate.rfile = io.BytesIO(handler._api_body) if handler.command in {"PUT", "POST"} else handler.rfile
    delegate.wfile = handler.wfile
    getattr(delegate, "do_" + handler.command)()
    return True
