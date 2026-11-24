from copy import deepcopy
import unittest

from .compact_evidence import validate_payload


class RunnerRestartProofTests(unittest.TestCase):
    def setUp(self):
        self.container = "a" * 64
        self.image = "sha256:" + "b" * 64
        before = {
            "Id": self.container,
            "Image": self.image,
            "RestartCount": 0,
            "State": {
                "StartedAt": "2026-11-23T04:15:07.357111797Z",
                "Running": True,
                "Health": {"Status": "healthy"},
            },
        }
        after = deepcopy(before)
        after["State"]["StartedAt"] = "2026-11-23T04:20:27.865011138Z"
        controller = {
            "kind": "actual-controller-process",
            "runner_id": 1,
            "pid": 10,
            "start_ticks": 7232715,
            "uid": 10001,
        }
        self.payload = {
            "before": before,
            "after": after,
            "controller_before": controller,
            "controller_after": {**controller, "start_ticks": 7264787},
        }

    def validate(self):
        validate_payload(
            "runner_restart",
            self.payload,
            "c" * 40,
            {},
            {"runner": self.image},
            {"runner": self.container},
        )

    def test_actual_manual_restart_with_unchanged_zero_counter_passes(self):
        self.validate()

    def test_counters_do_not_claim_monotonicity_for_operator_start(self):
        self.payload["before"]["RestartCount"] = 2
        self.validate()

    def test_boolean_counter_is_rejected(self):
        self.payload["after"]["RestartCount"] = False
        with self.assertRaisesRegex(ValueError, "nonnegative integer"):
            self.validate()

    def test_negative_counter_is_rejected(self):
        self.payload["before"]["RestartCount"] = -1
        with self.assertRaisesRegex(ValueError, "nonnegative integer"):
            self.validate()

    def test_float_counter_is_rejected(self):
        self.payload["after"]["RestartCount"] = 0.0
        with self.assertRaisesRegex(ValueError, "nonnegative integer"):
            self.validate()

    def test_unchanged_oci_start_is_rejected(self):
        self.payload["after"]["State"]["StartedAt"] = self.payload["before"]["State"][
            "StartedAt"
        ]
        with self.assertRaises(ValueError):
            self.validate()

    def test_unchanged_controller_process_is_rejected(self):
        self.payload["controller_after"]["start_ticks"] = self.payload[
            "controller_before"
        ]["start_ticks"]
        with self.assertRaises(ValueError):
            self.validate()

    def test_different_container_is_rejected(self):
        self.payload["after"]["Id"] = "d" * 64
        with self.assertRaises(ValueError):
            self.validate()

    def test_different_image_is_rejected(self):
        self.payload["after"]["Image"] = "sha256:" + "d" * 64
        with self.assertRaises(ValueError):
            self.validate()
