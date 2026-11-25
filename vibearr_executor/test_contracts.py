import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .contracts import Limits, Manifest, Profile, Rejected


def smoke_profiles(image):
    limits = Limits(1, 1024, 64, 512)
    return [
        Profile(
            "agent",
            1,
            image,
            "agent",
            ("python",),
            limits,
            ("api.openai.com",),
            "API_KEY",
            "",
        ),
        Profile("validation", 1, image, "validation", ("python",), limits, (), "", ""),
    ]


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.profile = smoke_profiles("sha256:" + "a" * 64)[0]
        self.manifest = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": str(uuid4()),
            "profile_id": self.profile.profile_id,
            "profile_digest": self.profile.digest,
            "source_digest": "b" * 64,
            "context_digest": "c" * 64,
            "phase": "agent",
            "limits": asdict(self.profile.limits),
            "deadline": (
                datetime.now(timezone.utc) + timedelta(minutes=10)
            ).isoformat(),
            "network_hosts": ["api.openai.com"],
        }

    def test_complete_manifest_roundtrips(self):
        self.assertEqual(
            Manifest.parse(self.manifest, self.profile).data(), self.manifest
        )

    def test_manifest_cannot_supply_executable_mount_or_host_path(self):
        for key in ("argv", "command", "mounts", "workspace_path", "image", "secret"):
            with self.subTest(key=key), self.assertRaises(Rejected):
                Manifest.parse({**self.manifest, key: "/host"}, self.profile)

    def test_manifest_cannot_expand_resource_limits(self):
        limits = {**self.manifest["limits"], "cpu": 2}
        with self.assertRaises(Rejected):
            Manifest.parse({**self.manifest, "limits": limits}, self.profile)

    def test_manifest_cannot_expand_network_allowlist(self):
        with self.assertRaises(Rejected):
            Manifest.parse(
                {**self.manifest, "network_hosts": ["example.com"]}, self.profile
            )

    def test_manifest_rejects_naive_and_unbounded_deadlines(self):
        for deadline in (
            datetime.now().isoformat(),
            (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            "yesterday",
        ):
            with self.subTest(deadline=deadline), self.assertRaises(Rejected):
                Manifest.parse({**self.manifest, "deadline": deadline}, self.profile)

    def test_boolean_resource_limits_are_rejected(self):
        with self.assertRaises(Rejected):
            Limits.parse({**asdict(Limits()), "cpu": True})

    def test_memory_accounts_for_tmpfs_workspace(self):
        with self.assertRaises(Rejected):
            Limits.parse({"cpu": 1, "pids": 32, "memory_mb": 128, "workspace_mb": 128})

    def test_profile_requires_pinned_image(self):
        with self.assertRaises(Rejected):
            Profile.parse({**self.profile.data(), "image": "python:latest"})

    def test_validation_profile_is_offline_and_credential_free(self):
        for changed in (
            {"network_hosts": ["pypi.org"]},
            {"backend_secret_env": "API_KEY"},
        ):
            profile = next(
                profile
                for profile in smoke_profiles("sha256:" + "a" * 64)
                if profile.phase == "validation"
            ).data()
            with self.subTest(changed=changed), self.assertRaises(Rejected):
                Profile.parse({**profile, **changed})

    def test_profile_rejects_ip_private_wildcard_and_url_hosts(self):
        for host in (
            "127.0.0.1",
            "::1",
            "metadata.internal",
            "host.local",
            "*.openai.com",
            "https://api.openai.com",
        ):
            with self.subTest(host=host), self.assertRaises(Rejected):
                Profile.parse({**self.profile.data(), "network_hosts": [host]})

    def test_profile_digest_binds_recipe_image_network_and_resources(self):
        for change in (
            {"argv": ["/different"]},
            {"image": "sha256:" + "d" * 64},
            {"network_hosts": []},
            {"limits": {**self.profile.data()["limits"], "pids": 32}},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(
                    Profile.parse({**self.profile.data(), **change}).digest,
                    self.profile.digest,
                )
