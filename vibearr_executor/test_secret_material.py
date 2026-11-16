import json
import unittest

from .secret_material import contains_secret, redact


class StructuredSecretTests(unittest.TestCase):
    def setUp(self):
        self.access = b"access-token-0123456789"
        self.refresh = b"refresh-token-9876543210"
        self.secret = json.dumps(
            {
                "tokens": {
                    "access_token": self.access.decode(),
                    "refresh_token": self.refresh.decode(),
                }
            }
        ).encode()

    def test_individual_structured_tokens_are_redacted(self):
        self.assertEqual(
            redact(self.access + b" " + self.refresh, self.secret),
            b"[REDACTED] [REDACTED]",
        )

    def test_partial_token_is_withheld_from_stream(self):
        self.assertEqual(
            redact(b"value:" + self.access[:12], self.secret, incomplete=True),
            b"value:",
        )

    def test_individual_token_in_export_is_detected(self):
        self.assertTrue(contains_secret(b"file content " + self.access, self.secret))

    def test_invalid_json_still_redacts_full_selected_secret(self):
        self.assertEqual(
            redact(b"prefix:key-not-json-123456789", b"key-not-json-123456789"),
            b"prefix:[REDACTED]",
        )

    def test_no_secret_preserves_output(self):
        self.assertEqual(redact(b"unchanged", b""), b"unchanged")

    def test_nonsecret_auth_metadata_does_not_reject_repository_source(self):
        selected = json.dumps(
            {
                "auth_mode": "chatgptAuthTokens",
                "last_refresh": "1970-01-01T00:00:00Z",
                "tokens": {"access_token": self.access.decode()},
            }
        ).encode()
        source = (
            b'auth_mode = "chatgptAuthTokens"\nlast_refresh = "1970-01-01T00:00:00Z"\n'
        )
        self.assertFalse(contains_secret(source, selected))
        self.assertEqual(redact(source, selected), source)
