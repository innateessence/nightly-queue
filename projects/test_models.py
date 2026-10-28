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
