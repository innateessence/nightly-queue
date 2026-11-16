from django.core.exceptions import ValidationError
from django.test import TestCase

from .models import Project


class ProjectTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo/",
            base_branch="develop",
        )

    def assert_invalid_repository(self, repository_url):
        self.project.repository_url = repository_url
        with self.assertRaises(ValidationError):
            self.project.save()

    def assert_invalid_branch(self, branch):
        self.project.base_branch = branch
        with self.assertRaises(ValidationError):
            self.project.save()

    def test_repository_is_normalized_to_clone_url(self):
        expected = "https://github.com/example/repo.git"
        self.assertEqual(self.project.repository_url, expected)

    def test_autonomous_defaults_to_false(self):
        self.assertIs(self.project.autonomous, False)

    def test_autonomous_can_be_enabled(self):
        self.project.autonomous = True
        self.project.save()
        self.project.refresh_from_db()
        self.assertIs(self.project.autonomous, True)

    def test_repository_rejects_non_github_host(self):
        self.assert_invalid_repository("https://gitlab.com/example/repo")

    def test_repository_requires_https(self):
        self.assert_invalid_repository("http://github.com/example/repo")

    def test_repository_rejects_credentials(self):
        self.assert_invalid_repository("https://token@github.com/example/repo")

    def test_repository_rejects_subpage(self):
        self.assert_invalid_repository("https://github.com/example/repo/issues")

    def test_repository_rejects_query_string(self):
        self.assert_invalid_repository("https://github.com/example/repo?token=secret")

    def test_repository_rejects_fragment(self):
        self.assert_invalid_repository("https://github.com/example/repo#main")

    def test_repository_rejects_empty_name(self):
        self.assert_invalid_repository("https://github.com/owner/.git")

    def test_repository_rejects_malformed_url(self):
        self.assert_invalid_repository("https://[invalid/owner/repo")

    def test_branch_rejects_leading_option(self):
        self.assert_invalid_branch("-option")

    def test_branch_rejects_double_dot(self):
        self.assert_invalid_branch("a..b")

    def test_branch_rejects_trailing_slash(self):
        self.assert_invalid_branch("feature/")

    def test_branch_rejects_hidden_component(self):
        self.assert_invalid_branch(".hidden")

    def test_branch_rejects_lock_suffix(self):
        self.assert_invalid_branch("a.lock")

    def test_branch_rejects_spaces(self):
        self.assert_invalid_branch("a b")

    def test_branch_rejects_reflog_expression(self):
        self.assert_invalid_branch("x@{y")

    def test_branch_rejects_backslash(self):
        self.assert_invalid_branch("x\\y")

    def test_stale_edit_preserves_server_owned_metadata_and_identity(self):
        import uuid
        from django.utils import timezone

        stale = Project.objects.get(pk=self.project.pk)
        fields = dict(
            github_repository_id=99,
            github_owner_id=42,
            github_default_branch="main",
            sync_lock_token=uuid.uuid4(),
            sync_lock_until=timezone.now(),
            sync_etags={"1": {"etag": "new"}},
            sync_success_at=timezone.now(),
            sync_error="provider backoff",
        )
        Project.objects.filter(pk=stale.pk).update(**fields)
        stale.name = "Edited name"
        stale.save()
        stale.refresh_from_db()
        self.assertEqual(stale.name, "Edited name")
        for key, value in fields.items():
            self.assertEqual(getattr(stale, key), value)
        stale.repository_url = "https://github.com/elsewhere/repoint"
        with self.assertRaisesMessage(ValidationError, "identified by GitHub ID"):
            stale.save()

    def test_stale_name_save_adopts_synchronized_canonical_rename(self):
        stale = Project.objects.get(pk=self.project.pk)
        Project.objects.filter(pk=stale.pk).update(
            github_repository_id=99,
            repository_url="https://github.com/example/renamed.git",
        )
        stale.name = "Renamed project"
        stale.save()
        self.assertEqual(stale.repository_url, "https://github.com/example/renamed.git")
        self.assertEqual(stale.github_repository_id, 99)
