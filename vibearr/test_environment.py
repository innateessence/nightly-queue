import importlib
import os
import unittest
from unittest.mock import patch
import warnings

from .environment import getenv


class EnvironmentTests(unittest.TestCase):
    def test_legacy_token_remains_usable(self):
        with patch.dict(os.environ, {"NQ_API_TOKEN": "existing"}, clear=True):
            self.assertEqual(getenv("NQ_API_TOKEN"), "existing")

    def test_canonical_token_takes_precedence_with_names_only_warning(self):
        values = {"VIBEARR_API_TOKEN": "new-secret", "NQ_API_TOKEN": "old-secret"}
        with patch.dict(os.environ, values, clear=True):
            with warnings.catch_warnings(record=True) as records:
                warnings.simplefilter("always")
                self.assertEqual(getenv("NQ_API_TOKEN"), "new-secret")
        message = str(records[0].message)
        self.assertIn("VIBEARR_API_TOKEN", message)
        self.assertIn("NQ_API_TOKEN", message)
        self.assertNotIn("new-secret", message)
        self.assertNotIn("old-secret", message)

    def test_equal_aliases_do_not_warn(self):
        values = {"VIBEARR_CREDENTIAL_KEY": "same", "NQ_CREDENTIAL_KEY": "same"}
        with patch.dict(os.environ, values, clear=True):
            with warnings.catch_warnings(record=True) as records:
                self.assertEqual(getenv("NQ_CREDENTIAL_KEY"), "same")
        self.assertEqual(records, [])

    def test_explicit_empty_canonical_value_disables_legacy_token(self):
        values = {"VIBEARR_API_TOKEN": "", "NQ_API_TOKEN": "old-secret"}
        with patch.dict(os.environ, values, clear=True):
            with self.assertWarns(RuntimeWarning):
                self.assertEqual(getenv("NQ_API_TOKEN"), "")

    def test_postgres_names_support_canonical_configuration(self):
        with patch.dict(os.environ, {"VIBEARR_POSTGRES_DB": "vibearr"}, clear=True):
            self.assertEqual(getenv("POSTGRES_DB", "nightly_queue"), "vibearr")

    def test_legacy_cli_import_is_the_same_module(self):
        self.assertIs(
            importlib.import_module("nq.cli"), importlib.import_module("vibearr.cli")
        )
