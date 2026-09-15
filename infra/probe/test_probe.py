import copy
import io
import unittest
from contextlib import redirect_stdout
from typing import Any
from unittest.mock import MagicMock, patch

from probe import PolicyClient, Tunnel, smoke, validate_config


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
    def test_actual_monitoring_api_response(self):
        server = MagicMock()
        server.client.request.side_effect = [b"synthetic", b'{"writtenMetricsCount":1}']
        with (patch("probe.identity_token", return_value="test-token"),
              patch("probe.Path.read_bytes", return_value=b"m1-private-object\n"),
              patch.dict("os.environ", {"ZONT_FOLDER_ID": "test-folder"}),
              redirect_stdout(io.StringIO())):
            smoke(server)
        self.assertEqual(server.client.request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
