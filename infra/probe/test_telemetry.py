import json
import unittest
from unittest.mock import Mock

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from telemetry import Telemetry


class TelemetryTest(unittest.TestCase):
    def config(self, endpoint="https://otlp-gateway-test.grafana.net/otlp"):
        return json.dumps({"endpoint": endpoint, "username": "123", "token": "test-token"})

    def test_payload_has_only_bounded_health_series(self):
        telemetry = Telemetry(self.config(), "dev")
        raw = telemetry.payload(True, 1.2)
        request = ExportMetricsServiceRequest.FromString(raw)
        metrics = request.resource_metrics[0].scope_metrics[0].metrics
        self.assertEqual([metric.name for metric in metrics],
                         ["zont_m1_probe_success", "zont_m1_probe_duration_seconds"])
        self.assertEqual(metrics[0].gauge.data_points[0].as_double, 1)
        self.assertEqual(metrics[1].gauge.data_points[0].as_double, 1.2)
        self.assertNotIn(b"test-token", raw)
        failed = ExportMetricsServiceRequest.FromString(telemetry.payload(False, 0.5))
        self.assertEqual(failed.resource_metrics[0].scope_metrics[0].metrics[0].gauge.data_points[0].as_double, 0)

    def test_credential_is_only_sent_to_validated_otlp_endpoint(self):
        telemetry = Telemetry(self.config(), "pilot")
        client = Mock()
        client.request.return_value = b""
        telemetry.send(client, True, 1)
        self.assertEqual(client.request.call_args.args[0], "https://otlp-gateway-test.grafana.net/otlp/v1/metrics")
        self.assertEqual(client.request.call_args.args[3]["Content-Type"], "application/x-protobuf")
        self.assertTrue(client.request.call_args.args[3]["Authorization"].startswith("Basic "))

    def test_partial_rejection_fails_export(self):
        client = Mock()
        response = ExportMetricsServiceResponse()
        response.partial_success.rejected_data_points = 1
        client.request.return_value = response.SerializeToString()
        with self.assertRaises(RuntimeError):
            Telemetry(self.config(), "dev").send(client, True, 1)

    def test_wrong_endpoints_and_environments_are_rejected(self):
        for endpoint in ("http://otlp-gateway-test.grafana.net/otlp",
                         "https://other.example/otlp", "https://stack.grafana.net/otlp",
                         "https://otlp-gateway-test.grafana.net.other.example/otlp",
                         "https://user:password@otlp-gateway-test.grafana.net/otlp",
                         "https://otlp-gateway-test.grafana.net/otlp?redirect=other"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                Telemetry(self.config(endpoint), "dev")
        with self.assertRaises(ValueError):
            Telemetry(self.config(), "prod")


if __name__ == "__main__":
    unittest.main()
