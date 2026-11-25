"""Small OTLP metric export without a resident collector or background thread."""
import base64
import json
import re
import time
from urllib.parse import urlsplit

from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)

MONIUM_HOST = "ingest.monium.yandex.cloud"
MONIUM_PATH = "/otlp/v1/logs"
PROBE_SERVICE = "zont-infrastructure-probe"


class Telemetry:
    def __init__(self, configuration, environment):
        config = json.loads(configuration)
        endpoint = urlsplit(config["endpoint"])
        if (endpoint.scheme != "https" or not endpoint.hostname
                or not endpoint.hostname.startswith("otlp-gateway-")
                or not endpoint.hostname.endswith(".grafana.net")
                or endpoint.port not in (None, 443) or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment
                or endpoint.path.rstrip("/") not in ("/otlp", "/otlp/v1/metrics")):
            raise ValueError("invalid Grafana OTLP endpoint")
        if environment not in ("dev", "pilot"):
            raise ValueError("isolated telemetry environment required")
        username, token = config["username"], config["token"]
        if not isinstance(username, str) or not username.isdigit():
            raise ValueError("invalid OTLP instance identifier")
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or "\n" in token or "\r" in token:
            raise ValueError("invalid OTLP credential")
        self.host = endpoint.hostname
        self.url = f"https://{self.host}/otlp/v1/metrics"
        self.authorization = "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
        self.environment = environment

    def payload(self, success, duration):
        request = ExportMetricsServiceRequest()
        resource = request.resource_metrics.add()
        resource.resource.attributes.add(key="service.name").value.string_value = PROBE_SERVICE
        resource.resource.attributes.add(key="deployment.environment.name").value.string_value = self.environment
        scope = resource.scope_metrics.add()
        scope.scope.name = "zont-m1-probe"
        timestamp = time.time_ns()
        for name, unit, value in (
            ("zont_m1_probe_success", "", float(success)),
            ("zont_m1_probe_duration_seconds", "s", float(duration)),
        ):
            metric = scope.metrics.add(name=name, unit=unit)
            point = metric.gauge.data_points.add(time_unix_nano=timestamp, as_double=value)
            point.attributes.add(key="environment").value.string_value = self.environment
        return request.SerializeToString()

    def send(self, client, success, duration):
        body = client.request(self.url, "POST", self.payload(success, duration),
                              {"Authorization": self.authorization, "Content-Type": "application/x-protobuf"})
        response = ExportMetricsServiceResponse()
        response.ParseFromString(body)
        if response.partial_success.rejected_data_points:
            raise RuntimeError("Grafana rejected metric points")


class MoniumLogs:
    def __init__(self, project, environment, revision):
        self.project = project
        self.environment = environment
        self.revision = revision

    def send(self, client, token, phases):
        request = ExportLogsServiceRequest()
        resource = request.resource_logs.add()
        resource.resource.attributes.add(key="service.name").value.string_value = PROBE_SERVICE
        if self.environment in ("dev", "pilot"):
            resource.resource.attributes.add(key="deployment.environment.name").value.string_value = self.environment
        if re.fullmatch(r"[0-9a-f]{40}", self.revision):
            resource.resource.attributes.add(key="service.version").value.string_value = self.revision
        scope = resource.scope_logs.add()
        scope.scope.name = "zont-m1-probe"
        for event in phases:
            level = event["level"]
            record = scope.log_records.add(time_unix_nano=time.time_ns(), severity_text=level,
                                           severity_number=9 if level == "INFO" else 17)
            record.body.string_value = "m1-smoke"
            for key in ("event", "smoke_id", "phase", "status", "error_type"):
                if key in event:
                    record.attributes.add(key=key).value.string_value = event[key]
        body = client.request(f"https://{MONIUM_HOST}{MONIUM_PATH}", "POST", request.SerializeToString(), {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-protobuf",
            "x-monium-project": self.project,
            "x-monium-service": PROBE_SERVICE,
        }, timeout=2, max_bytes=16384)
        response = ExportLogsServiceResponse.FromString(body)
        if response.partial_success.rejected_log_records:
            raise RuntimeError("Monium rejected log records")
