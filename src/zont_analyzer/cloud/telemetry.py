"""Synchronous, bounded OTLP/JSON gauges for cloud job outcomes."""
from __future__ import annotations

import base64
import json
import multiprocessing
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from zont_analyzer.observability import Measurement


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
        self.instance = str(uuid.uuid4())
        self.started = time.time_ns()
        self._lock = threading.Lock()
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], tuple[float, int]] = {}
        self._exported: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    def record(self, measurement: Measurement) -> None:
        name, value, labels = measurement
        key = name, tuple(sorted(labels.items()))
        with self._lock:
            if key not in self._values and len(self._values) >= 256:
                return
            previous = self._values.get(key, (0.0, 0))[0]
            self._values[key] = (previous + value if name.endswith("_total") else value, time.time_ns())

    def payload(self) -> dict[str, Any]:
        stamp = time.time_ns()
        grouped: dict[str, dict[str, Any]] = {}
        with self._lock:
            values = dict(self._values)
            exported = set(self._exported)
        for (name, labels), (value, observed) in values.items():
            counter = name.endswith("_total")
            kind = "sum" if counter else "gauge"
            metric = grouped.setdefault(name, {"name": name, kind: {"dataPoints": []}})
            point: dict[str, Any] = {
                "timeUnixNano": str(stamp if counter else observed), "asDouble": value,
                "attributes": [{"key": key, "value": {"stringValue": item}}
                               for key, item in (("environment", self.environment), *labels)],
            }
            if counter:
                metric[kind].update(aggregationTemporality=2, isMonotonic=True)
                point["startTimeUnixNano"] = str(self.started)
                if (name, labels) not in exported:
                    metric[kind]["dataPoints"].append({
                        **point, "timeUnixNano": str(self.started), "asDouble": 0.0,
                    })
            metric[kind]["dataPoints"].append(point)
        return {"resourceMetrics": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "zont-cloud-runtime"}},
            {"key": "service.instance.id", "value": {"stringValue": self.instance}},
        ]}, "scopeMetrics": [{"scope": {"name": "zont-cloud-jobs"},
                              "metrics": list(grouped.values())}]}]}

    def send(self, success: bool, duration: float) -> None:
        self.record(("zont_cloud_job_success", float(success), {}))
        self.record(("zont_cloud_job_duration_seconds", duration, {}))
        body = self.payload()
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
            with self._lock:
                self._exported.update(self._values)
        finally:
            if process.is_alive():
                process.kill()
                process.join(1)
            process.close()
