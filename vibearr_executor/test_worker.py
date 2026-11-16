import unittest

from .worker import redact_logs, redact_result


class RedactionTests(unittest.TestCase):
    def test_complete_key_ending_in_its_own_prefix_is_redacted(self):
        secret = b"sk-123456789012345s"
        self.assertEqual(redact_logs(secret, secret), b"[REDACTED]")

    def test_repeated_prefix_does_not_disclose_a_complete_or_partial_key(self):
        secret = b"a" * 16
        self.assertEqual(redact_logs(secret + b"a" * 8, secret), b"[REDACTED]")

    def test_stream_redaction_preserves_previously_returned_byte_offsets(self):
        secret = b"sk-123456789012345s"
        chunks = [b"before ", secret[:5], secret[5:-1], secret[-1:], b" after"]
        content, previous = b"", b""
        for chunk in chunks:
            content += chunk
            current = redact_logs(content, secret)
            self.assertTrue(current.startswith(previous))
            self.assertNotIn(secret, current)
            previous = current
        self.assertEqual(previous, b"before [REDACTED] after")

    def test_final_partial_key_is_withheld(self):
        secret = b"selected-secret-123456"
        self.assertEqual(redact_logs(b"message " + secret[:-1], secret), b"message ")

    def test_result_redaction_handles_quoted_and_nested_values(self):
        secret = 'selected-"quoted"-secret'
        result = {"error": secret, "files": [{"path": "before/" + secret}]}
        self.assertEqual(
            redact_result(result, secret),
            {"error": "[REDACTED]", "files": [{"path": "before/[REDACTED]"}]},
        )
