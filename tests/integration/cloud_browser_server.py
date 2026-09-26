"""Isolated cloud HTTP/browser fixture: real YDB, in-memory private Object Storage."""
from __future__ import annotations

import base64
import hashlib
import os
import threading
from datetime import date
from pathlib import Path

import httpx

from zont_analyzer.application import publication as publication_module
from zont_analyzer.application.gas import GasService
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.application.publication import publish_reports
from zont_analyzer.cloud import runtime as cloud_runtime
from zont_analyzer.cloud.object_storage import ObjectStorage
from zont_analyzer.cloud.runtime import CloudServer, RuntimeConfig
from zont_analyzer.reports import render_text
from zont_analyzer.runtime import build_runtime

BUCKET = "browser-fixture-bucket"
PREFIX = "browser"
objects: dict[str, tuple[bytes, str, str]] = {}
object_lock = threading.Lock()


def object_request(request: httpx.Request) -> httpx.Response:
    path_prefix = f"/{BUCKET}/{PREFIX}/"
    if not request.url.path.startswith(path_prefix) or request.headers.get("Authorization") != "Bearer fixture":
        return httpx.Response(403)
    assert request.headers.get("Accept-Encoding") == "identity"
    key = request.url.path[len(path_prefix):]
    with object_lock:
        existing = objects.get(key)
        if request.method == "PUT":
            assert request.headers.get("If-None-Match") == "*"
            assert request.headers.get("Cache-Control") == "private, no-store"
            if existing is not None:
                return httpx.Response(412)
            body = request.content
            etag = hashlib.sha256(body).hexdigest()
            content_type = request.headers.get("Content-Type", "application/octet-stream")
            objects[key] = (body, content_type, etag)
            return httpx.Response(201, headers={"etag": etag})
        if existing is None:
            return httpx.Response(404)
        body, content_type, etag = existing
        if request.method == "HEAD":
            return httpx.Response(200, headers={"etag": etag, "content-type": content_type})
        if request.method == "GET":
            return httpx.Response(200, content=body, headers={"etag": etag, "content-type": content_type})
        return httpx.Response(405)


storage = ObjectStorage(BUCKET, PREFIX, token=lambda: "fixture", transport=httpx.MockTransport(object_request))
ObjectStorage.from_environment = classmethod(lambda cls: storage)
os.environ["CLOUD_PUBLICATION_BUCKET"] = BUCKET
os.environ["CLOUD_PUBLICATION_PREFIX"] = PREFIX
publication_module.reports_directory = lambda *_: (_ for _ in ()).throw(
    AssertionError("cloud publication touched the filesystem"))

# The browser fixture has no model credentials. Make an unexpected AI construction fail loudly.
from zont_analyzer.adapters.openai import OpenAIAnalyst  # noqa: E402

OpenAIAnalyst.__init__ = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("AI in browser fixture"))

runtime = build_runtime(None, Path("/tmp/cloud-browser-data"))
try:
    runtime.db.save_devices([{
        "device_id": "browser-synthetic-device", "name": "Browser synthetic boiler",
        "model": "Synthetic boiler", "_equipment": {},
    }])
    OwnerContextStore(runtime.db).update_profile("browser-synthetic-device", {
        "fields": {"installation_notes": {"value": "<b>synthetic owner note</b>"}},
    })
    analysis = runtime.analysis(no_ai=True)
    # Preserve synthetic presentation evidence while testing publication and browser routes.
    GasService.refresh = lambda self, report: report

    def save_with_gas(report, volume: float) -> None:
        report.context["gas"] = {
            "status": "measured", "volume_m3": volume, "coverage_pct": 100,
            "reliability_index_pct": 80, "observed_days": 1, "complete": True,
        }
        runtime.db.save_report(report, render_text(report))

    daily = analysis.analyze_daily(date(2026, 8, 5), use_ai=False)
    daily.summary = "Synthetic <script>unsafe</script> report"
    # Persisted synthetic metadata exercises provenance without an AI request.
    daily.ai_used = True
    daily.context["ai_provenance"] = {
        "requested_model": "fixture-model", "response_model": "fixture-model",
        "generated_at": daily.generated_at.isoformat(),
        "parameters": {"reasoning_effort": "low"},
        "prompt_version": "fixture-prompt", "schema_version": "fixture-schema",
    }
    daily.context["gas"] = {
        "status": "measured", "volume_m3": 12.3, "coverage_pct": 100,
        "reliability_index_pct": 80, "observed_days": 1, "complete": True,
        "cost": {"status": "available", "amounts": [{"currency": "RUB", "amount": "98.40"}]},
    }
    runtime.db.save_report(daily, render_text(daily))
    save_with_gas(analysis.analyze_week(2026, 31, use_ai=False), 80)
    save_with_gas(analysis.analyze_month(2026, 7, use_ai=False), 300)
    save_with_gas(analysis.analyze_season(2026, "spring", use_ai=False), 900)
    for _ in range(3):
        result = publish_reports(runtime)
        if result["pending_reports"] == 0:
            break
    assert result["reports"] == 4 and result["pending_reports"] == 0, result
finally:
    runtime.db.close()

# Keep the transport and YDB in this process while exercising the real HTTP job route.
# Production uses a bounded child; this fixture calls the same dispatcher directly so
# an in-memory immutable object map remains visible to the browser between requests.
cloud_runtime.run_bounded = lambda dispatcher, payload, _timeout, _cancelled: dispatcher(payload)


class ReadyTunnel:
    def ready(self) -> bool:
        return True


config = RuntimeConfig(
    environment="dev", port=8080, job_timeout_seconds=15, report_timeout_seconds=180,
    revision="cloud-browser-fixture",
    authorization="Basic " + base64.b64encode(b"browser:fixture-secret").decode("ascii"),
)
server = CloudServer(("0.0.0.0", 8080), config, ReadyTunnel())
print("cloud browser fixture ready", flush=True)
server.serve_forever()
