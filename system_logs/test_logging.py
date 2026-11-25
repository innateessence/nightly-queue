import json
import logging
import sys
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import quote

from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve
from django.utils import timezone

from credentials.models import Credential
from credentials.test_models import TEST_KEY
from work_queue.tasks import import_auto_issues

from .handlers import SystemLogHandler
from .models import SystemLog


@override_settings(VIBEARR_CREDENTIAL_KEY=TEST_KEY)
class SystemLogHandlerTests(TestCase):
    def emit(
        self,
        message,
        *,
        level=logging.INFO,
        source="work_queue.tasks",
        exc_info=None,
        extra=None,
    ):
        record = logging.LogRecord(source, level, __file__, 1, message, (), exc_info)
        record.__dict__.update(extra or {})
        SystemLogHandler().handle(record)
        return SystemLog.objects.first()

    def credential(self, secret="private-log-token"):
        credential = Credential(name="Log fixture", provider="github", kind="token")
        credential.set_secret(secret)
        credential.save()
        return credential

    def test_configured_application_logger_persists_formatted_event(self):
        logging.getLogger("work_queue.tasks").info("Project %s synced.", 42)
        entry = SystemLog.objects.get()
        self.assertEqual(entry.message, "Project 42 synced.")
        self.assertEqual(entry.source, "work_queue.tasks")
        self.assertEqual(entry.level, "info")

    def test_issue_sync_creates_a_real_system_log(self):
        import_auto_issues()
        self.assertIn("Auto issue import skipped", SystemLog.objects.get().message)

    def test_configured_credentials_are_redacted_before_storage(self):
        credential = self.credential()
        entry = self.emit("Access private-log-token " + credential.encrypted_payload)
        self.assertEqual(entry.message, "Access [redacted] [redacted]")

    def test_rotated_and_revoked_credentials_are_redacted(self):
        credential = self.credential("original-private-token")
        credential.set_secret("replacement-private-token")
        credential.save()
        credential.revoke()
        entry = self.emit("original-private-token replacement-private-token")
        self.assertNotIn("private-token", entry.message)

    def test_retained_deleted_credential_is_redacted(self):
        credential = self.credential()
        Credential.all_objects.filter(pk=credential.pk).update(
            deleted_at=timezone.now()
        )
        entry = self.emit("private-log-token")
        self.assertEqual(entry.message, "[redacted]")

    @override_settings(
        VIBEARR_API_TOKEN="queue-private-key", SECRET_KEY="django-private-key"
    )
    def test_application_configuration_secrets_are_redacted(self):
        entry = self.emit("queue-private-key django-private-key")
        self.assertEqual(entry.message, "[redacted] [redacted]")

    def test_structured_session_tokens_are_redacted(self):
        material = json.dumps(
            {
                "tokens": {
                    "access_token": "session-access",
                    "refresh_token": "session-refresh",
                    "id_token": "session-id",
                    "account_id": "session-account",
                }
            }
        )
        credential = Credential(name="Session", provider="codex", kind="session")
        credential.set_secret(material)
        credential.save()
        entry = self.emit("session-access session-refresh session-id")
        self.assertEqual(entry.message, "[redacted] [redacted] [redacted]")

    def test_encoded_secret_variants_are_redacted(self):
        credential = Credential(name="Secret", provider="github", kind="token")
        credential.set_secret('private/secret"value')
        credential.save()
        entry = self.emit(
            quote('private/secret"value', safe="")
            + " "
            + json.dumps('private/secret"value')[1:-1]
        )
        self.assertEqual(entry.message, "[redacted] [redacted]")

    def test_common_unconfigured_tokens_and_passwords_are_redacted(self):
        entry = self.emit(
            'Bearer abcdef Authorization: Basic ZXhhbXBsZQ== password="unknown secret" ghp_exampletoken'
        )
        for secret in ["abcdef", "ZXhhbXBsZQ==", "unknown secret", "ghp_exampletoken"]:
            self.assertNotIn(secret, entry.message)

    def test_redaction_failure_withholds_entire_record(self):
        Credential.objects.create(
            name="Broken", provider="github", kind="token", encrypted_payload="broken"
        )
        entry = self.emit("private-value", source="credentials.private-value")
        self.assertEqual(entry.source, "application")
        self.assertNotIn("private-value", entry.message)
        self.assertEqual(entry.details, "")

    def test_exception_details_omit_exception_values_and_frame_locals(self):
        try:
            raise ValueError("unconfigured-private-value")
        except ValueError:
            entry = self.emit(
                "Operation failed.", level=logging.ERROR, exc_info=sys.exc_info()
            )
        self.assertIn("ValueError", entry.details)
        self.assertNotIn("unconfigured-private-value", entry.details)
        self.assertEqual(entry.level, "error")

    def test_request_logs_omit_path_query_headers_and_exception_values(self):
        request = RequestFactory().get(
            "/logs/?q=private-value", HTTP_AUTHORIZATION="Bearer private-value"
        )
        request.resolver_match = resolve("/logs/")
        entry = self.emit(
            "Internal Server Error: /private-value",
            level=logging.ERROR,
            source="django.request",
            extra={"request": request, "status_code": 500},
        )
        self.assertEqual(entry.message, "HTTP 500 · logs/")
        self.assertNotIn("private-value", entry.message)

    def test_capture_does_not_raise_or_break_callers_transaction_when_storage_fails(
        self,
    ):
        with patch.object(
            SystemLog.objects, "create", side_effect=DatabaseError("private failure")
        ):
            self.emit("Safe event")
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            self.assertEqual(cursor.fetchone(), (1,))

    def test_failed_database_write_rolls_back_its_savepoint(self):
        with patch(
            "system_logs.handlers.safe_record",
            return_value=("source", "bad\x00message", ""),
        ):
            self.emit("Safe event")
        self.assertFalse(connection.needs_rollback)
        self.assertFalse(SystemLog.objects.exists())

    def test_logging_inside_broken_transaction_does_not_raise(self):
        with transaction.atomic():
            transaction.set_rollback(True)
            record = logging.LogRecord(
                "work_queue.tasks",
                logging.ERROR,
                __file__,
                1,
                "Failed operation",
                (),
                None,
            )
            SystemLogHandler().handle(record)
        self.assertFalse(connection.needs_rollback)

    def test_real_request_warning_is_captured_without_request_data(self):
        self.client.get("/missing-private-path/?token=private-query")
        entry = SystemLog.objects.get(source="django.request")
        self.assertEqual(entry.message, "HTTP 404 · unmatched route")
        self.assertNotIn("private", entry.message)

    def test_debug_records_are_excluded(self):
        self.emit("Debug", level=logging.DEBUG)
        self.assertFalse(SystemLog.objects.exists())

    def test_runner_output_is_not_collected(self):
        logging.getLogger("vibearr_runner.runtime").info("Runner-only event")
        self.assertFalse(SystemLog.objects.exists())

    def test_capture_prunes_expired_entries(self):
        expired = SystemLog.objects.create(
            level="info",
            source="fixture",
            message="Expired",
            created_at=timezone.now() - timedelta(days=8),
        )
        self.emit("Current event")
        self.assertFalse(SystemLog.objects.filter(pk=expired.pk).exists())

    @patch("system_logs.handlers.MAX_ENTRIES", 3)
    def test_capture_prunes_oldest_entries_to_retention_limit(self):
        for number in range(5):
            self.emit(f"Event {number}")
        self.assertEqual(
            list(SystemLog.objects.values_list("message", flat=True)),
            ["Event 4", "Event 3", "Event 2"],
        )

    def test_oversized_records_are_withheld(self):
        entry = self.emit("x" * 70000)
        self.assertIn("withheld", entry.message)
        self.assertLess(len(entry.message), 4000)
