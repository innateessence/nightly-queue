from .test_support import CONTROLLER_SECRET, provision_controller
import copy
import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .auth import token_digest
from .configuration import configuration_document
from .gate_models import RunnerGateEvidence
from .models import RunnerIdentity


class GateEvidenceTests(TestCase):
    def setUp(self):
        self.runner = RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="test-runner",
            token_digest=token_digest("gate-test-token"),
        )
        provision_controller(self)
        self.auth = {
            "HTTP_AUTHORIZATION": "Bearer gate-test-token",
            "HTTP_X_NQ_CONTROLLER_SECRET": CONTROLLER_SECRET,
        }
        now = timezone.now()
        self.report = {
            "schema_version": 1,
            "allowed": False,
            "reasons": ["release_disabled", "usage_reserve"],
            "checked_at": now.isoformat(),
            "checkpoint": "claim",
            "run_id": None,
            "configuration_revision": configuration_document()["revision"],
            "interval": {
                "id": "day:UTC:2026-11-16",
                "start": now.isoformat(),
                "end": (now + timedelta(days=1)).isoformat(),
            },
            "next_boundary": (now + timedelta(days=1)).isoformat(),
            "next_check_at": (now + timedelta(seconds=60)).isoformat(),
            "reserve_enabled": True,
            "minimum_remaining_usage_percent": 40,
            "tasks_started": 0,
            "readings": [
                {
                    "backend": "fake",
                    "credential_id": 1,
                    "status": "reserve",
                    "windows": [
                        {
                            "window_id": "weekly",
                            "remaining_percent": 40,
                            "observed_at": now.isoformat(),
                            "reset_at": (now + timedelta(days=1)).isoformat(),
                            "unit": "percent",
                            "status": "reserve",
                        }
                    ],
                }
            ],
        }

    def post(self, report=None, **kwargs):
        return self.client.post(
            reverse("runner_gate_report"),
            json.dumps({"report": report or self.report}),
            content_type="application/json",
            **(kwargs or self.auth),
        )

    def test_reports_require_runner_authentication(self):
        self.assertEqual(
            self.post(HTTP_AUTHORIZATION="Bearer generic-token").status_code, 401
        )

    def test_read_latest_requires_runner_authentication(self):
        self.assertEqual(self.client.get(reverse("runner_gates")).status_code, 401)

    def test_empty_evidence_is_explicit(self):
        response = self.client.get(reverse("runner_gates"), **self.auth)
        self.assertEqual(response.json(), {"schema_version": 1, "evidence": None})

    def test_report_is_durable_and_roundtrips(self):
        self.assertEqual(self.post().status_code, 200)
        response = self.client.get(reverse("runner_gates"), **self.auth)
        self.assertEqual(response.json()["evidence"]["report"], self.report)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_repeated_report_is_retry_safe(self):
        self.post()
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(RunnerGateEvidence.objects.count(), 1)

    def test_same_timestamp_cannot_change_evidence(self):
        self.post()
        changed = copy.deepcopy(self.report)
        changed["tasks_started"] = 1
        self.assertEqual(self.post(changed).status_code, 409)

    def test_old_retry_cannot_replace_newer_evidence(self):
        old = copy.deepcopy(self.report)
        old["checked_at"] = (timezone.now() - timedelta(minutes=5)).isoformat()
        self.post()
        self.assertEqual(self.post(old).status_code, 200)
        self.assertEqual(RunnerGateEvidence.objects.get().report, self.report)

    def test_report_does_not_expose_other_runners_evidence(self):
        self.post()
        RunnerIdentity.objects.create(
            controller_digest=token_digest(CONTROLLER_SECRET),
            name="other",
            token_digest=token_digest("other-token"),
        )
        response = self.client.get(
            reverse("runner_gates"), HTTP_AUTHORIZATION="Bearer other-token"
        )
        self.assertIsNone(response.json()["evidence"])

    def test_extra_secret_fields_are_rejected(self):
        self.report["claim_token"] = "must-not-persist"
        self.assertEqual(self.post().status_code, 400)
        self.assertFalse(RunnerGateEvidence.objects.exists())

    def test_nested_extra_secret_fields_are_rejected(self):
        self.report["readings"][0]["secret"] = "must-not-persist"
        self.assertEqual(self.post().status_code, 400)

    def test_arbitrary_blocking_text_is_rejected(self):
        self.report["reasons"] = ["provider exception secret"]
        self.assertEqual(self.post().status_code, 400)

    def test_future_check_timestamp_is_rejected(self):
        self.report["checked_at"] = (timezone.now() + timedelta(days=1)).isoformat()
        self.assertEqual(self.post().status_code, 400)

    def test_naive_timestamp_is_rejected(self):
        self.report["checked_at"] = "2026-11-16T23:00:00"
        self.assertEqual(self.post().status_code, 400)

    def test_unknown_run_is_rejected(self):
        self.report["checkpoint"] = "stage"
        self.report["run_id"] = "680ae6ae-9f9e-48d1-a0d9-6774980dc184"
        self.assertEqual(self.post().status_code, 400)

    def test_oversized_report_is_rejected(self):
        self.report["readings"][0]["windows"][0]["window_id"] = "x" * 65536
        self.assertEqual(self.post().status_code, 400)

    def test_invalid_usage_values_are_rejected(self):
        self.report["readings"][0]["windows"][0]["remaining_percent"] = 101
        self.assertEqual(self.post().status_code, 400)

    def test_null_characters_in_window_ids_are_rejected(self):
        self.report["readings"][0]["windows"][0]["window_id"] = "private\x00window"
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private", response.content.decode())
        self.assertFalse(RunnerGateEvidence.objects.exists())

    def test_unpaired_surrogates_in_window_ids_are_rejected(self):
        self.report["readings"][0]["windows"][0]["window_id"] = "private\ud800window"
        response = self.post()
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("private", response.content.decode())
        self.assertFalse(RunnerGateEvidence.objects.exists())

    def test_window_ids_preserve_valid_unicode_and_literal_json_escapes(self):
        identity = "Weekly \U0001f600 \u00e9 " + r"\u0000"
        self.report["readings"][0]["windows"][0]["window_id"] = identity
        self.assertEqual(self.post().status_code, 200)
        evidence = RunnerGateEvidence.objects.get()
        self.assertEqual(
            evidence.report["readings"][0]["windows"][0]["window_id"], identity
        )

    def test_settings_show_reason_remaining_freshness_reset_and_boundary(self):
        self.post()
        response = self.client.get(reverse("coding_runner_edit", args=[1]))
        for text in (
            "test-runner",
            "usage_reserve",
            "40% remaining",
            "Refreshed",
            "Reset",
            "Next schedule boundary",
        ):
            with self.subTest(text=text):
                self.assertContains(response, text)

    def test_settings_mark_expired_reports(self):
        self.report["checked_at"] = (timezone.now() - timedelta(minutes=5)).isoformat()
        self.report["next_check_at"] = (
            timezone.now() - timedelta(minutes=4)
        ).isoformat()
        self.post()
        self.assertContains(
            self.client.get(reverse("coding_runner_edit", args=[1])), "Report expired"
        )

    def test_settings_mark_changed_configuration(self):
        self.report["configuration_revision"] = "previous-revision"
        self.post()
        self.assertContains(
            self.client.get(reverse("coding_runner_edit", args=[1])),
            "Configuration changed since this report",
        )
