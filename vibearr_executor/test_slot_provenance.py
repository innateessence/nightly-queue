from dataclasses import replace
import unittest
from uuid import uuid4

from .contracts import Rejected, digest
from .validation_profile import ValidationProfile, validate_dependency_provenance


class SlotProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.parent = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, digest(b"base")
        )
        self.profile = replace(self.parent, dependency_digest=digest(b"candidate"))
        build_id = str(uuid4())
        self.value = {
            "schema_version": 2,
            "build_id": build_id,
            "run_id": str(uuid4()),
            "source_digest": "c" * 64,
            "requirements_digest": self.profile.dependency_digest,
            "dependency_digest": "d" * 64,
            "parent_image": self.parent.image,
            "parent_profile_digest": self.parent.digest,
            "parent_runtime_binding": "e" * 64,
            "setup_operation_id": build_id,
            "setup_log_digest": "f" * 64,
            "artifact": {
                "schema_version": 1,
                "sha256": "d" * 64,
                "requirements_digest": self.profile.dependency_digest,
                "bytes": 10240,
                "files": 4,
            },
            "validation_profile_digest": self.profile.digest,
            "setup_instance_id": str(uuid4()),
            "setup_worker_boot": str(uuid4()),
        }

    def test_artifact_changes_dependencies_without_inventing_an_image(self):
        result = validate_dependency_provenance(
            self.value, self.profile, parent=self.parent
        )
        self.assertEqual(result["parent_image"], self.profile.image)
        self.assertNotIn("derived_image", result)

    def test_artifact_cannot_cross_run_ownership(self):
        with self.assertRaises(Rejected):
            validate_dependency_provenance(
                self.value, self.profile, run_id=str(uuid4())
            )

    def test_different_payload_hash_is_rejected(self):
        self.value["artifact"]["sha256"] = "0" * 64
        with self.assertRaises(Rejected):
            validate_dependency_provenance(self.value, self.profile)

    def test_candidate_dependencies_cannot_replace_the_trusted_image(self):
        changed = replace(self.profile, image="sha256:" + "0" * 64)
        self.value["validation_profile_digest"] = changed.digest
        with self.assertRaises(Rejected):
            validate_dependency_provenance(self.value, changed, parent=self.parent)
