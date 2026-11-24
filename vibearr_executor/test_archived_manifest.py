from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from .contracts import Manifest, Rejected
from .qualify import smoke_profiles


class ArchivedManifestTests(unittest.TestCase):
    def setUp(self):
        self.profile = smoke_profiles("sha256:" + "a" * 64)[0]
        self.started = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.manifest = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": self.profile.profile_id,
            "profile_digest": self.profile.digest,
            "source_digest": "b" * 64,
            "context_digest": "c" * 64,
            "phase": self.profile.phase,
            "limits": asdict(self.profile.limits),
            "deadline": (self.started + timedelta(minutes=10)).isoformat(),
            "network_hosts": ["api.openai.com"],
        }

    def test_expired_manifest_validates_at_its_historical_start(self):
        self.assertEqual(
            Manifest.parse_archived(
                self.manifest, self.profile, self.started.timestamp()
            ).data(),
            self.manifest,
        )

    def test_live_parser_still_rejects_the_same_expired_manifest(self):
        with self.assertRaisesRegex(Rejected, "next hour"):
            Manifest.parse(self.manifest, self.profile)

    def test_live_manifest_cannot_supply_an_archival_clock(self):
        value = {**self.manifest, "started_at": self.started.timestamp()}
        with self.assertRaisesRegex(Rejected, "complete versioned"):
            Manifest.parse(value, self.profile)

    def test_archived_start_must_be_a_finite_positive_sealed_timestamp(self):
        for value in (
            None,
            True,
            "2024-01-01T00:00:00Z",
            datetime(2024, 1, 1),
            float("nan"),
            float("inf"),
            0,
            -1,
            10**100,
        ):
            with self.subTest(value=value), self.assertRaises(Rejected):
                Manifest.parse_archived(self.manifest, self.profile, value)

    def test_archived_deadline_must_be_aware_and_within_one_hour_of_start(self):
        for deadline in (
            datetime(2024, 1, 1).isoformat(),
            "yesterday",
            (self.started + timedelta(hours=2)).isoformat(),
            self.started.isoformat(),
        ):
            with self.subTest(deadline=deadline), self.assertRaises(Rejected):
                Manifest.parse_archived(
                    {**self.manifest, "deadline": deadline},
                    self.profile,
                    self.started.timestamp(),
                )

    def test_archival_parser_keeps_immutable_profile_checks(self):
        with self.assertRaisesRegex(Rejected, "registered immutable"):
            Manifest.parse_archived(
                {**self.manifest, "profile_digest": "d" * 64},
                self.profile,
                self.started.timestamp(),
            )

    def test_archival_parser_keeps_resource_and_network_checks(self):
        for value in (
            {"limits": {**self.manifest["limits"], "cpu": 999}},
            {"network_hosts": ["example.com"]},
        ):
            with self.subTest(value=value), self.assertRaises(Rejected):
                Manifest.parse_archived(
                    {**self.manifest, **value}, self.profile, self.started.timestamp()
                )

    def test_reconciliation_parser_requires_a_finite_positive_sealed_observation(self):
        for value in (None, False, float("nan"), float("inf"), -1):
            with self.subTest(value=value), self.assertRaises(Rejected):
                Manifest.parse_reconciled(self.manifest, self.profile, value)
