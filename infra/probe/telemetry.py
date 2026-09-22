"""Small OTLP metric export without a resident collector or background thread."""
import base64
import json
import time
from urllib.parse import urlsplit

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)


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
        resource.resource.attributes.add(key="service.name").value.string_value = "zont-infrastructure-probe"
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
