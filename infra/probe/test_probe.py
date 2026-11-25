import copy
import io
import unittest
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import MagicMock, patch

from probe import Handler, PolicyClient, Tunnel, smoke, validate_config


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.client = PolicyClient({"proxy.example"}, {"direct.example"})
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.isclosed.return_value = False
        self.response.read1.side_effect = [b"hello", b""]
        self.factory = patch("probe.http.client.HTTPSConnection", return_value=self.connection)
        self.constructor = self.factory.start()
        self.addCleanup(self.factory.stop)

    def test_tls_tunnel_keeps_target_and_path(self):
        self.assertEqual(self.client.request("https://proxy.example/check?v=1"), b"hello")
        self.constructor.assert_called_once()
        self.assertEqual(self.constructor.call_args.args[:2], ("127.0.0.1", 1080))
        self.connection.set_tunnel.assert_called_once_with("proxy.example", 443)
        self.assertEqual(self.connection.request.call_args.args[:2], ("GET", "/check?v=1"))

    def test_direct_route_does_not_tunnel(self):
        self.client.request("https://direct.example/")
        self.assertEqual(self.constructor.call_args.args[:2], ("direct.example", 443))
        self.connection.set_tunnel.assert_not_called()

    def test_fully_read_response_can_close_its_socket(self):
        self.response.isclosed.side_effect = [False, True]
        self.response.read1.side_effect = [b"hello"]
        self.assertEqual(self.client.request("https://direct.example/"), b"hello")
        self.response.read1.assert_called_once()

    def test_proxy_failure_never_creates_direct_connection(self):
        self.connection.connect.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.client.request("https://proxy.example/")
        self.constructor.assert_called_once()
        self.connection.close.assert_called_once()

    def test_denied_destinations_never_connect(self):
        for url in ("https://unknown.example/", "http://proxy.example/",
                    "https://user:secret@proxy.example/", "https://proxy.example:80/",
                    "https://proxy.example/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.client.request(url)
        self.constructor.assert_not_called()

    def test_redirect_is_rejected_without_followup(self):
        self.response.status = 302
        with self.assertRaises(RuntimeError):
            self.client.request("https://proxy.example/")
        self.connection.request.assert_called_once()
        self.response.read1.assert_not_called()

    def test_monium_route_permits_only_direct_logs_post_with_http_200(self):
        host = "ingest.monium.yandex.cloud"
        url = f"https://{host}/otlp/v1/logs"
        client = PolicyClient({"proxy.example"}, {host})
        for target, method in ((url, "GET"), (url + "?v=1", "POST"), (url + "/elsewhere", "POST")):
            with self.subTest(url=target, method=method), self.assertRaises(ValueError):
                client.request(target, method)
        with self.assertRaises(ValueError):
            PolicyClient({host}).request(url, "POST")
        self.constructor.assert_not_called()

        self.response.status = 204
        with self.assertRaises(RuntimeError):
            client.request(url, "POST")
        self.constructor.assert_called_once()
        self.connection.set_tunnel.assert_not_called()

    def test_request_and_response_share_byte_budget(self):
        self.response.read1.side_effect = [b"abc", b"d"]
        with self.assertRaises(ValueError):
            self.client.request("https://proxy.example/", "POST", b"12", max_bytes=5)
        self.connection.close.assert_called_once()

    def test_timeout_releases_connection_slot(self):
        self.connection.connect.side_effect = TimeoutError()
        for _ in range(5):
            with self.assertRaises(TimeoutError):
                self.client.request("https://proxy.example/")

    def test_environment_cannot_override_route(self):
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://other.example", "NO_PROXY": "*"}):
            self.client.request("https://proxy.example/")
        self.connection.set_tunnel.assert_called_once_with("proxy.example", 443)


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.config: dict[str, Any] = {
            "inbounds": [{"listen": "127.0.0.1", "protocol": "http", "port": 1080}],
            "outbounds": [{"protocol": "vless"}],
        }

    def test_config_disables_logs(self):
        validate_config(self.config, 1080)
        self.assertEqual(self.config["log"]["loglevel"], "none")

    def test_public_inbound_and_direct_outbound_rejected(self):
        changes: dict[str, Any]
        for changes in (
            {"inbounds": [{"listen": "0.0.0.0", "protocol": "http", "port": 1080}]},
            {"outbounds": [{"protocol": "freedom"}]},
            {"inbounds": [{"listen": "127.0.0.1", "protocol": "socks", "port": 1080}]},
            {"api": {}},
        ):
            config = copy.deepcopy(self.config)
            config.update(changes)
            with self.assertRaises(ValueError):
                validate_config(config, 1080)

    def test_ready_requires_process_and_port(self):
        tunnel = object.__new__(Tunnel)
        tunnel.port = 1080
        tunnel.process = MagicMock()
        tunnel.process.poll.return_value = None
        with patch("probe.socket.create_connection", side_effect=OSError()):
            self.assertFalse(tunnel.ready())
        tunnel.process.poll.return_value = 1
        with patch("probe.socket.create_connection") as connect:
            self.assertFalse(tunnel.ready())
            connect.assert_not_called()


class SmokeTests(unittest.TestCase):
    def test_identity_storage_and_egress_without_legacy_metric_write(self):
        server = MagicMock()
        server.client.request.return_value = b"synthetic"
        with (patch("probe.identity_token", return_value="test-token"),
              patch("probe.Path.read_bytes", return_value=b"m1-private-object\n"),
              redirect_stdout(io.StringIO())):
            self.assertEqual(smoke(server, []), "test-token")
        server.client.request.assert_called_once_with(server.smoke_url)


class HandlerTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(Handler)
        self.handler.connection = MagicMock()
        self.handler.headers = {}  # type: ignore[assignment]
        self.handler.rfile = io.BytesIO()
        self.handler.path = "/smoke"
        self.handler.command = "POST"
        self.handler.server = MagicMock()
        self.reply = MagicMock()
        self.handler.reply = self.reply  # type: ignore[method-assign]
        self.server: Any = self.handler.server
        self.server.monium = None

    def test_success_requires_metric_delivery(self):
        with patch("probe.smoke") as check:
            self.handler.handle_request()
        check.assert_called_once_with(self.server, [])
        self.assertTrue(self.server.telemetry.send.call_args.args[1])
        self.reply.assert_called_once_with(200, None, accepted=None)

    def test_smoke_acceptance_correlates_with_terminal_log(self):
        self.server.monium = MagicMock()
        with patch("probe.smoke", return_value="metadata-token"):
            self.handler.handle_request()

        sent = self.server.monium.send.call_args.args
        self.assertEqual(sent[0:2], (self.server.client, "metadata-token"))
        self.assertEqual(sent[2], [{
            "event": "probe_smoke", "phase": "complete", "level": "INFO", "status": "ok",
            "smoke_id": self.reply.call_args.kwargs["accepted"],
        }])
        self.assertEqual(self.reply.call_args.args[0], 200)

    def test_log_delivery_failure_preserves_probe_result_and_redacts_details(self):
        self.server.monium = MagicMock()
        self.server.monium.send.side_effect = ValueError("private token")
        output = io.StringIO()
        with patch("probe.smoke", return_value="metadata-token"), redirect_stdout(output):
            self.handler.handle_request()
        self.reply.assert_called_once_with(200, None, accepted=None)
        self.assertNotIn("private token", output.getvalue())

    def test_failed_check_exports_zero_without_leaking_error(self):
        with patch("probe.smoke", side_effect=ValueError("private upstream credential")):
            self.handler.handle_request()
        self.assertFalse(self.server.telemetry.send.call_args.args[1])
        self.assertEqual(self.reply.call_args.args[0], 502)
        self.assertNotIn(b"credential", self.reply.call_args.args[1])

    def test_delivery_failure_does_not_report_success_or_retry(self):
        self.server.telemetry.send.side_effect = RuntimeError("private token")
        with patch("probe.smoke"):
            self.handler.handle_request()
        self.server.telemetry.send.assert_called_once()
        self.assertEqual(self.reply.call_args.args[0], 502)
        self.assertIn(b"grafana", self.reply.call_args.args[1])
        self.assertNotIn(b"token", self.reply.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
