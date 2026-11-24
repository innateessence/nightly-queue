from dataclasses import replace
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from .contracts import canonical, digest
from .validation_profile import TASK_ARGV
from . import test_compact_worker as worker_tests


class CompactDatabasePolicyTests(unittest.TestCase):
    setUp = worker_tests.CompactWorkerTests.setUp

    def prepare_database(self, profile, *, context=None):
        document = {"adapter": "codex", "factory_role": "build", **(context or {})}
        content = canonical(document)
        self.runtime.upload("context", content, digest(content))
        manifest = {
            **self.manifest,
            "operation_id": str(uuid4()),
            "profile_id": profile.profile_id,
            "profile_digest": profile.digest,
            "phase": profile.phase,
            "limits": profile.data()["limits"],
            "network_hosts": list(profile.network_hosts),
            "context_digest": digest(content),
        }
        self.runtime.prepare(manifest)
        self.runtime.database = Mock()
        return manifest["operation_id"]

    def django_profile(self):
        return next(
            profile
            for profile in self.runtime.profiles.values()
            if profile.argv == (*TASK_ARGV, "django")
        )

    def granted(self, operation):
        self.runtime.prepare_database(operation, time.time() + 20)
        return self.runtime.database.prepare.call_args.kwargs["allow_createdb"]

    def test_exact_registered_django_validation_gets_createdb(self):
        operation = self.prepare_database(self.django_profile())
        self.assertIs(self.granted(operation), True)

    def test_ordinary_agent_and_context_flags_cannot_grant_createdb(self):
        operation = self.prepare_database(
            self.profile,
            context={
                "allow_createdb": True,
                "check": "django",
                "profile_id": self.django_profile().profile_id,
            },
        )
        self.assertIs(self.granted(operation), False)

    def test_other_registered_validation_commands_deny_createdb(self):
        profiles = [
            profile
            for profile in self.runtime.profiles.values()
            if profile.phase == "validation"
            and profile.database_image
            and profile.argv != (*TASK_ARGV, "django")
        ]
        for profile in profiles:
            with self.subTest(profile=profile.profile_id):
                operation = self.prepare_database(profile)
                self.assertIs(self.granted(operation), False)

    def test_browser_profile_denies_createdb(self):
        profile = replace(
            self.django_profile(), profile_id="browser-ui", argv=("/trusted/browser",)
        )
        self.runtime.profiles[profile.profile_id] = profile
        self.assertIs(self.granted(self.prepare_database(profile)), False)

    def test_qualification_cannot_grant_createdb_even_for_django_recipe(self):
        operation = self.prepare_database(self.django_profile())
        self.runtime.qualifying = True
        self.assertIs(self.granted(operation), False)

    def test_lookalike_registered_id_with_different_command_denies_createdb(self):
        expected = self.django_profile()
        profile = replace(expected, argv=("/trusted/unregistered-test",))
        self.runtime.profiles[profile.profile_id] = profile
        self.assertIs(self.granted(self.prepare_database(profile)), False)

    def test_different_worker_image_revision_denies_createdb(self):
        expected = self.django_profile()
        profile = replace(expected, image="sha256:" + "c" * 64)
        self.runtime.profiles[profile.profile_id] = profile
        self.assertIs(self.granted(self.prepare_database(profile)), False)

    def test_changed_manifest_digest_denies_createdb(self):
        operation = self.prepare_database(self.django_profile())
        record = self.runtime.record(operation)
        record["manifest"]["profile_digest"] = "e" * 64
        self.runtime.save(record)
        self.assertIs(self.granted(operation), False)

    def test_changed_prepared_boundary_denies_createdb(self):
        operation = self.prepare_database(self.django_profile())
        record = self.runtime.record(operation)
        record["execution_identity"]["worker_boot"] = str(uuid4())
        self.runtime.save(record)
        self.assertIs(self.granted(operation), False)

    def test_missing_trusted_validation_document_denies_createdb(self):
        operation = self.prepare_database(self.django_profile())
        self.runtime.validation_profile = None
        self.assertIs(self.granted(operation), False)
