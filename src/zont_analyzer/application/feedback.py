from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

from zont_analyzer.application.publication import publish_report, publish_reports
from zont_analyzer.application.regeneration import normalize_counterfactual_question
from zont_analyzer.application.regeneration import start as start_regeneration
from zont_analyzer.application.regeneration import status as regeneration_status
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
        "experiment": value.get("experiment"),
    }


def publish_feedback_report(runtime: Runtime, report_id: str) -> None:
    """Refresh public HTML artifacts after a lifecycle write."""
    publish_report(runtime, report_id)


class FeedbackHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_feedback_server(runtime: Runtime) -> FeedbackHttpServer:
    api_path = _api_path(runtime)
    route_prefix = f"{api_path}/recommendations/"

    class Handler(BaseHTTPRequestHandler):
        server_version = "ZontAnalyzerFeedback/1"

        def _ai_request(self, *, write: bool = False) -> bool:
            from zont_analyzer.adapters.openai.model_catalog import OpenAIModelCatalog
            from zont_analyzer.application.ai_maintenance import local_assessments, review_state, start_review
            from zont_analyzer.application.ai_settings import AISettingsStore
            from zont_analyzer.application.model_review import ModelReviewStore

            path = urlsplit(self.path).path
            if path not in {f"{api_path}/ai", f"{api_path}/ai/review"}:
                return False
            settings = AISettingsStore(runtime.db, runtime.config)
            status = HTTPStatus.OK
            try:
                if write:
                    if not self._same_origin_write():
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": "Откройте форму на сайте приложения."})
                        return True
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError("Тело запроса должно быть JSON.")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= size <= MAX_REQUEST_BYTES:
                        raise ValueError("Некорректный размер запроса.")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("Ожидается JSON-объект.")
                    if path.endswith("/review"):
                        if set(payload) - {"action", "proposal_id", "expected_version"}:
                            raise ValueError("Неизвестные поля действия.")
                        if payload.get("action") == "check":
                            start_review(runtime, manual=True)
                            status = HTTPStatus.ACCEPTED
                        else:
                            if (not isinstance(payload.get("proposal_id"), str)
                                    or type(payload.get("expected_version")) is not int
                                    or payload.get("action") not in {"accept", "reject", "defer"}):
                                raise ValueError("Укажите предложение, его версию и действие.")
                            catalog = OpenAIModelCatalog()
                            try:
                                ModelReviewStore(runtime.db, catalog, assessments=local_assessments(runtime)).decide(
                                    payload["proposal_id"], payload["action"], payload["expected_version"], settings,
                                )
                            finally:
                                catalog.close()
                    else:
                        settings.save(payload)
                value = settings.view()
                value["review"] = review_state(runtime)
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Предложение не найдено."})
                return True
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                return True
            self._send_json(status, value)
            return True

        def _owner_request(self, *, write: bool = False) -> bool:
            """Equipment and meter writes share the existing loopback/Basic Auth perimeter."""
            from zont_analyzer.application.owner_context import OwnerContextStore

            request = urlsplit(self.path)
            path = request.path
            query = parse_qs(request.query, keep_blank_values=True)
            equipment_prefix = f"{api_path}/equipment/"
            gas_prefix = f"{api_path}/reports/"
            kind = ""
            identifier = ""
            if path == f"{api_path}/equipment":
                kind = "profiles"
            elif path == f"{api_path}/gas-tariffs":
                kind, identifier = "tariffs", "installation"
            elif path.startswith(equipment_prefix):
                kind, identifier = "profile", unquote(path[len(equipment_prefix):])
            elif path.startswith(gas_prefix) and path.endswith("/gas"):
                kind, identifier = "gas", unquote(path[len(gas_prefix):-4])
            else:
                return False
            if kind != "profiles" and (not identifier or "/" in identifier):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Маршрут не найден."})
                return True
            store = OwnerContextStore(runtime.db)
            try:
                if write:
                    if kind == "profiles":
                        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Укажите устройство."})
                        return True
                    origin = self.headers.get("Origin")
                    if (self.headers.get("Sec-Fetch-Site") == "cross-site"
                            or origin is not None and urlsplit(origin).hostname !=
                            urlsplit("http://" + self.headers.get("Host", "")).hostname):
                        self._send_json(HTTPStatus.FORBIDDEN, {"error": "Откройте форму на сайте приложения."})
                        return True
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError("Тело запроса должно быть JSON.")
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= size <= MAX_REQUEST_BYTES:
                        raise ValueError("Некорректный размер запроса.")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("Ожидается JSON-объект.")
                    effective = payload.get("effective_from")
                    if kind == "profile" and isinstance(effective, str) and len(effective) == 10:
                        payload["effective_from"] = datetime.fromisoformat(effective).replace(
                            tzinfo=ZoneInfo(runtime.config.home.effective_timezone)
                        ).astimezone(UTC).isoformat()
                    if kind == "tariffs":
                        from zont_analyzer.application.gas_tariffs import GasTariffStore

                        tariffs = GasTariffStore(runtime.db, timezone=runtime.config.home.effective_timezone)
                        value = tariffs.save(payload)
                        value["history"] = tariffs.history()
                    else:
                        value = (store.update_profile(identifier, payload) if kind == "profile"
                                 else store.update_gas(identifier, payload))
                    try:
                        if kind == "tariffs":
                            from zont_analyzer.application.publication import publish_tariff_change

                            if not value.get("idempotent"):
                                publish_tariff_change(runtime, value["affected_start"], value["affected_end"])
                        elif kind == "profile":
                            publish_reports(runtime)
                        # Gas writes are already durable. The regular worker
                        # publication reads current readings and refreshes the
                        # archive, including calibrated/comparison contexts.
                        # Never make this response wait for the publication lock
                        # or another full archive pass; restart/retry is covered
                        # by the next successful worker cycle.
                    except (OSError, ValueError):
                        value["publish_warning"] = "Сохранено; HTML обновится в следующем цикле."
                elif kind == "profiles":
                    value = {"profiles": [store.profile(str(d["id"])) for d in runtime.db.list_devices()]}
                elif kind == "tariffs":
                    from zont_analyzer.application.gas_tariffs import GasTariffStore

                    value = {"history": GasTariffStore(runtime.db).history()}
                else:
                    if kind == "profile":
                        value = store.profile(identifier)
                    else:
                        days = query.get("day", [])
                        if len(days) > 1:
                            raise ValueError("Укажите одну дату показания в формате YYYY-MM-DD.")
                        value = store.gas(identifier, days[0] if days else None)
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Устройство или дневной отчёт не найдены."})
                return True
            except (ValueError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                return True
            self._send_json(HTTPStatus.OK, value)
            return True

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

        def _regeneration_id(self) -> str | None:
            path = urlsplit(self.path).path
            prefix = f"{api_path}/reports/"
            suffix = "/regenerate"
            if not path.startswith(prefix) or not path.endswith(suffix):
                return None
            encoded_id = path[len(prefix):-len(suffix)]
            if not encoded_id or "/" in encoded_id:
                return None
            return unquote(encoded_id)

        def _same_origin_write(self) -> bool:
            origin = self.headers.get("Origin")
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            return not (
                self.headers.get("Sec-Fetch-Site") == "cross-site"
                or origin is not None and urlsplit(origin).hostname != host
            )

        def do_GET(self) -> None:  # noqa: N802
            if self._ai_request():
                return
            if urlsplit(self.path).path == f"{api_path}/health":
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if self._owner_request():
                return
            regeneration_id = self._regeneration_id()
            if regeneration_id is not None:
                if runtime.db.report(regeneration_id) is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Отчёт не найден."})
                else:
                    self._send_json(HTTPStatus.OK, regeneration_status(runtime, regeneration_id))
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
            if self._ai_request(write=True):
                return
            if self._owner_request(write=True):
                return
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
            if not isinstance(payload, dict) or set(payload) - {"status", "owner_note", "experiment"}:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Допустимы только status, owner_note и experiment."})
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
                experiment = payload.get("experiment")
                if isinstance(experiment, dict) and experiment.get("performed_at"):
                    when = datetime.fromisoformat(experiment["performed_at"])
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=ZoneInfo(runtime.config.home.effective_timezone))
                    experiment = {**experiment, "performed_at": when.astimezone(UTC).isoformat()}
                value = runtime.db.set_recommendation_feedback(
                    recommendation_id, status, owner_note, experiment=experiment,
                )
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Рекомендация не найдена."})
                return
            except (ValueError, TypeError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                return
            response = _public_feedback(value)
            try:
                publish_feedback_report(runtime, str(value["report_id"]))
            except (OSError, ValueError) as exc:
                logger.warning("Feedback saved but report republish failed: %s", type(exc).__name__)
                response["publish_warning"] = "Обратная связь сохранена, HTML обновится в следующем цикле."
            self._send_json(HTTPStatus.OK, response)

        def do_POST(self) -> None:  # noqa: N802
            regeneration_id = self._regeneration_id()
            if regeneration_id is None:
                self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Используйте PUT."})
                return
            if not self._same_origin_write():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Откройте отчёт на сайте приложения."})
                return
            if self.headers.get_content_type() != "application/json":
                self._send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Тело запроса должно быть JSON."})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > MAX_REQUEST_BYTES:
                    raise ValueError("Некорректный размер запроса.")
                if size:
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict) or set(payload) - {"question"}:
                        raise ValueError("Допустим только необязательный вопрос.")
                    question = normalize_counterfactual_question(payload.get("question"))
                else:
                    question = None
                value = (
                    start_regeneration(runtime, regeneration_id, question)
                    if question is not None
                    else start_regeneration(runtime, regeneration_id)
                )
            except KeyError:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Отчёт не найден."})
                return
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.ACCEPTED, value)

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
