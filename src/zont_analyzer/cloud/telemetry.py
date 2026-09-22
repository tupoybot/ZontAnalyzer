"""Synchronous, bounded OTLP/JSON gauges for cloud job outcomes."""
from __future__ import annotations

import base64
import json
import multiprocessing
import sys
import time
from urllib.parse import urlsplit

import httpx


def _export(url: str, authorization: str, body: dict[str, object]) -> None:
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=1) as client:
            response = client.post(url, json=body, headers={"Authorization": authorization})
            if response.status_code != 200:
                raise RuntimeError("metrics export failed")
            partial = response.json().get("partialSuccess", {})
            if int(partial.get("rejectedDataPoints", 0)):
                raise RuntimeError("metrics rejected")
    except Exception:
        sys.exit(1)


class Telemetry:
    def __init__(self, raw: str, environment: str) -> None:
        config = json.loads(raw)
        endpoint = urlsplit(config["endpoint"])
        if (endpoint.scheme != "https" or not endpoint.hostname
                or not endpoint.hostname.startswith("otlp-gateway-")
                or not endpoint.hostname.endswith(".grafana.net")
                or endpoint.port not in (None, 443) or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment
                or endpoint.path.rstrip("/") not in ("/otlp", "/otlp/v1/metrics")):
            raise ValueError("invalid OTLP destination")
        if environment not in ("dev", "pilot"):
            raise ValueError("isolated environment required")
        username, token = config["username"], config["token"]
        if not isinstance(username, str) or not username.isdigit():
            raise ValueError("invalid metric identity")
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c in token for c in "\r\n"):
            raise ValueError("invalid metric credential")
        self.url = f"https://{endpoint.hostname}/otlp/v1/metrics"
        self.authorization = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        self.environment = environment

    def send(self, success: bool, duration: float) -> None:
        stamp = str(time.time_ns())
        metrics = [{"name": name, "unit": unit, "gauge": {"dataPoints": [{
            "timeUnixNano": stamp, "asDouble": value,
            "attributes": [{"key": "environment", "value": {"stringValue": self.environment}}],
        }]}} for name, unit, value in (
            ("zont_cloud_job_success", "", float(success)),
            ("zont_cloud_job_duration_seconds", "s", duration),
        )]
        body = {"resourceMetrics": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "zont-cloud-runtime"}},
        ]}, "scopeMetrics": [{"scope": {"name": "zont-cloud-jobs"}, "metrics": metrics}]}]}
        process = multiprocessing.get_context("spawn").Process(
            target=_export, args=(self.url, self.authorization, body),
        )
        process.start()
        try:
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join(1)
                raise TimeoutError("metrics deadline exceeded")
            if process.exitcode != 0:
                raise RuntimeError("metrics export failed")
        finally:
            if process.is_alive():
                process.kill()
                process.join(1)
            process.close()
