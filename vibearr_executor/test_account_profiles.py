import unittest

from vibearr_runner.native_codex import codex_profile
from vibearr_runner.policy import ExecutionPolicy

from .slot_profiles import profiles
from .slot_proxy import HOSTS


class AccountProfileTests(unittest.TestCase):
    def setUp(self):
        self.image = "sha256:" + "a" * 64
        self.database = "sha256:" + "b" * 64
        self.profiles = profiles("agent", self.image, self.database, "c" * 64)

    def test_default_worker_recipe_uses_the_codex_account(self):
        selected = {item.profile_id: item for item in self.profiles}
        profile = selected["native-default"]
        expected = codex_profile(
            self.image,
            "session",
            profile_id="native-default",
            database_image=self.database,
        )
        self.assertEqual(profile, expected)

    def test_existing_session_profile_is_registered(self):
        self.assertEqual(
            {item.profile_id for item in self.profiles},
            {"native-default", "codex-session-v1"},
        )

    def test_account_worker_does_not_register_an_api_key_recipe(self):
        token = codex_profile(
            self.image,
            "token",
            profile_id="native-default",
            database_image=self.database,
        )
        self.assertNotIn(token.digest, {item.digest for item in self.profiles})

    def test_task_proxy_allows_only_the_account_inference_host(self):
        self.assertEqual(HOSTS["agent"], {"chatgpt.com"})

    def test_default_project_policy_allows_the_account_endpoint(self):
        self.assertIn("chatgpt.com", ExecutionPolicy().network_hosts)
        self.assertNotIn("api.openai.com", ExecutionPolicy().network_hosts)
