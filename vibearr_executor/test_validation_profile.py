from .validation_profile import validate_dependency_provenance
import unittest
from dataclasses import replace
from unittest.mock import patch

from .contracts import Rejected
from . import validation_app, validation_task
from .validation_profile import (
    CHECK_IDS,
    ValidationProfile,
)


class ValidationProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64
        )

    def test_every_required_check_has_fixed_offline_credential_free_recipe(self):
        profiles = self.profile.executor_profiles()
        self.assertEqual(len(profiles), len(CHECK_IDS))
        for check, profile in zip(CHECK_IDS, profiles):
            self.assertEqual(profile.argv[-1], check)
            self.assertEqual(profile.phase, "validation")
            self.assertEqual(profile.network_hosts, ())
            self.assertEqual(profile.backend_secret_env, "")

    def test_profile_round_trip_does_not_need_engine_access(self):
        self.assertEqual(ValidationProfile.parse(self.profile.data()), self.profile)

    def test_candidate_cannot_remove_required_checks(self):
        data = self.profile.data()
        data["required_checks"] = []
        with self.assertRaises(Rejected):
            ValidationProfile.parse(data)

    def test_retired_cli_check_cannot_be_added_to_the_profile(self):
        data = self.profile.data()
        data["required_checks"].append("cli")
        with self.assertRaises(Rejected):
            ValidationProfile.parse(data)

    def test_candidate_cannot_replace_command_arguments(self):
        data = self.profile.data()
        data["executor_profiles"][0]["argv"] = ["/bin/true"]
        with self.assertRaises(Rejected):
            ValidationProfile.parse(data)

    def test_other_image_has_distinct_executor_profile_identity(self):
        changed = replace(self.profile, image="sha256:" + "d" * 64)
        self.assertNotEqual(
            self.profile.executor_profiles()[0].profile_id,
            changed.executor_profiles()[0].profile_id,
        )

    def test_validation_requires_a_pinned_disposable_database(self):
        with self.assertRaises(Rejected):
            replace(self.profile, database_image="")

    def test_image_build_provenance_cannot_replace_dependency_artifact_provenance(self):
        with self.assertRaises(Rejected):
            validate_dependency_provenance(
                {"schema_version": 1, "derived_image": self.profile.image}, self.profile
            )


class ValidationEntrypointTests(unittest.TestCase):
    def test_retired_cli_task_is_rejected_before_setup(self):
        with patch("sys.argv", ["trusted-validation", "cli"]):
            with self.assertRaisesRegex(RuntimeError, "registered validation check"):
                validation_task.main()

    def test_retired_cli_application_check_is_rejected_before_setup(self):
        with patch("sys.argv", ["trusted-validation", "cli"]):
            with self.assertRaisesRegex(RuntimeError, "fixed application validation"):
                validation_app.main()

    def test_retired_cli_suite_cannot_alias_git_acceptance(self):
        with self.assertRaisesRegex(RuntimeError, "fixed runtime validation suite"):
            validation_app.suite("cli")
