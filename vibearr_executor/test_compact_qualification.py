import unittest
from unittest.mock import patch

from .compact_qualification import qualification_targets
from .contracts import Rejected


class QualificationTargetTests(unittest.TestCase):
    def setUp(self):
        self.targets = {"runner": "172.23.0.2", "app": "172.24.0.2", "postgres": "172.25.0.2"}
        resolver = patch("vibearr_executor.compact_qualification.socket.getaddrinfo")
        self.resolve = resolver.start()
        self.addCleanup(resolver.stop)
        self.resolve.return_value = [(2, 1, 6, "", (self.targets["runner"], 8792))]

    def test_actual_private_service_targets_are_preserved(self):
        self.assertEqual(qualification_targets(self.targets), self.targets)

    def test_missing_actual_target_is_rejected(self):
        del self.targets["postgres"]
        with self.assertRaises(Rejected):
            qualification_targets(self.targets)

    def test_loopback_substitute_is_rejected(self):
        self.targets["postgres"] = "127.0.0.1"
        with self.assertRaises(Rejected):
            qualification_targets(self.targets)

    def test_reusing_one_service_address_is_rejected(self):
        self.targets["app"] = self.targets["runner"]
        with self.assertRaises(Rejected):
            qualification_targets(self.targets)

    def test_runner_target_must_resolve_to_the_actual_service(self):
        self.targets["runner"] = "172.23.0.3"
        with self.assertRaises(Rejected):
            qualification_targets(self.targets)
