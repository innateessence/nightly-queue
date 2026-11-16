import unittest
from dataclasses import replace
from uuid import uuid4

from .contracts import Rejected, digest
from .qualification import REQUIRED_CHECKS
from .validation_profile import (
    CHECK_IDS,
    ValidationProfile,
    validate_dependency_provenance,
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

    def provenance(self):
        build = str(uuid4())
        return {
            "schema_version": 1,
            "build_id": build,
            "run_id": str(uuid4()),
            "source_digest": "d" * 64,
            "requirements_digest": self.profile.dependency_digest,
            "dependency_digest": "e" * 64,
            "parent_image": "sha256:" + "f" * 64,
            "parent_profile_digest": "a" * 64,
            "parent_runtime_binding": "b" * 64,
            "setup_operation_id": build,
            "setup_log_digest": "c" * 64,
            "derived_image": self.profile.image,
            "qualification_binding_digest": "d" * 64,
            "qualification_report_digest": "e" * 64,
            "boundary_checks": sorted(REQUIRED_CHECKS),
        }

    def test_dependency_provenance_requires_full_real_boundary_check_set(self):
        value = self.provenance()
        self.assertEqual(validate_dependency_provenance(value, self.profile), value)
        value["boundary_checks"].pop()
        with self.assertRaises(Rejected):
            validate_dependency_provenance(value, self.profile)

    def test_dependency_provenance_cannot_change_its_run(self):
        with self.assertRaises(Rejected):
            validate_dependency_provenance(
                self.provenance(), self.profile, run_id=str(uuid4())
            )

    def test_dependency_provenance_cannot_claim_other_requirements(self):
        value = self.provenance()
        value["requirements_digest"] = digest(b"other")
        with self.assertRaises(Rejected):
            validate_dependency_provenance(value, self.profile)
