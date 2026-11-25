import unittest
from unittest.mock import patch

from .client import Client
from .contracts import Rejected


class DedicatedWorkerOriginTests(unittest.TestCase):
    def test_external_executor_origin_is_rejected(self):
        with self.assertRaises(Rejected):
            Client("https://external.example", "x" * 48)

    def test_retired_worker_fleet_alias_is_rejected(self):
        with patch.dict("os.environ", {"VIBEARR_RUNTIME_PROTOCOL": "compose-slot-v2"}):
            with self.assertRaises(Rejected):
                Client("http://agent-worker:8791", "x" * 48)

    def test_worker_dns_cannot_resolve_to_public_control(self):
        with patch(
            "vibearr_executor.client.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("1.1.1.1", 8791))],
        ):
            with self.assertRaises(Rejected):
                Client("http://worker:8791", "x" * 48)

    def test_private_worker_origin_uses_the_current_protocol(self):
        with patch(
            "vibearr_executor.client.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("172.21.0.2", 8791))],
        ):
            client = Client("http://worker:8791", "x" * 48)
        self.assertEqual(client.expected_runtime, "compact-worker-v1")

    def test_loopback_control_uses_the_current_protocol(self):
        self.assertEqual(
            Client("http://127.0.0.1:8791", "x" * 48).expected_runtime,
            "compact-worker-v1",
        )
