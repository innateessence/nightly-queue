import socket
import unittest
from unittest.mock import patch

from .proxy import resolve_public


class ProxyTests(unittest.TestCase):
    def resolve(self, *addresses):
        records = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            for address in addresses
        ]
        return patch("socket.getaddrinfo", return_value=records)

    def test_private_lan_metadata_loopback_and_reserved_are_denied(self):
        for address in (
            "127.0.0.1",
            "10.0.0.1",
            "172.17.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "0.0.0.0",
            "100.64.0.1",
            "224.0.0.1",
        ):
            with (
                self.subTest(address=address),
                self.resolve(address),
                self.assertRaises(ValueError),
            ):
                resolve_public("api.openai.com", set())

    def test_any_private_dns_answer_rejects_the_whole_resolution(self):
        with self.resolve("8.8.8.8", "127.0.0.1"), self.assertRaises(ValueError):
            resolve_public("api.openai.com", set())

    def test_host_public_address_is_denied(self):
        with self.resolve("8.8.8.8"), self.assertRaises(ValueError):
            resolve_public("api.openai.com", {"8.8.8.8"})

    def test_validated_numeric_addresses_are_returned_for_pinned_connect(self):
        with self.resolve("8.8.8.8", "1.1.1.1"):
            self.assertEqual(
                resolve_public("api.openai.com", set()), ["1.1.1.1", "8.8.8.8"]
            )
