from django.test import Client, TestCase, override_settings
from django.urls import reverse

from credentials.models import Credential
from credentials.test_models import TEST_KEY
from work_queue.services import claim_next, enqueue_item

from .models import Project


class ProjectViewTests(TestCase):
    def setUp(self):
        self.data = {
            "name": "Nightly",
            "repository_url": "https://github.com/example/nightly",
            "base_branch": "develop",
        }
        self.url = reverse("project_list")

    def create_project(self):
        return Project.objects.create(**self.data)

    @override_settings(NQ_CREDENTIAL_KEY=TEST_KEY)
    def create_credential(self, name="GitHub work account"):
        credential = Credential(name=name, kind=Credential.Kind.TOKEN)
        credential.set_secret("private-token")
        credential.save()
        return credential

    def assert_invalid_field(self, field, value):
        self.data[field] = value
        response = self.client.post(self.url, self.data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(field, response.context["form"].errors)
        self.assertContains(response, "Please check your project details.")
        self.assertFalse(Project.objects.exists())

    def test_empty_list_explains_how_to_add_project(self):
        response = self.client.get(self.url)
        self.assertContains(response, "No projects yet.")
        self.assertContains(response, "Create project")

    def test_list_shows_project_details_and_edit_link(self):
        project = self.create_project()
        response = self.client.get(self.url)
        self.assertContains(response, project.name)
        self.assertContains(response, project.repository_url)
        self.assertContains(response, "<code>develop</code>")
        self.assertContains(response, f"Project #{project.pk}")
        self.assertContains(response, reverse("edit_project", args=[project.pk]))

    def test_blank_branch_displays_repository_default(self):
        self.data["base_branch"] = ""
        self.create_project()
        response = self.client.get(self.url)
        self.assertContains(response, "<code>Repository default</code>")

    def test_create_normalizes_repository_and_redirects(self):
        response = self.client.post(self.url, self.data, follow=True)
        self.assertRedirects(response, self.url)
        project = Project.objects.get()
        self.assertEqual(project.name, "Nightly")
        self.assertEqual(project.repository_url, self.data["repository_url"] + ".git")
        self.assertEqual(project.base_branch, "develop")
        self.assertContains(response, "Project created.")

    def test_base_branch_is_optional(self):
        del self.data["base_branch"]
        self.client.post(self.url, self.data)
        self.assertEqual(Project.objects.get().base_branch, "")

    def test_create_form_lists_credential_names_without_secrets(self):
        credential = self.create_credential()
        response = self.client.get(self.url)
        self.assertContains(response, 'name="credential"')
        option = f'<option value="{credential.pk}">{credential.name}</option>'
        self.assertContains(response, option, html=True)
        self.assertNotContains(response, "private-token")
        self.assertNotContains(response, credential.encrypted_payload)

    def test_create_assigns_selected_credential(self):
        credential = self.create_credential()
        self.data["credential"] = credential.pk
        response = self.client.post(self.url, self.data)
        self.assertRedirects(response, self.url)
        self.assertEqual(Project.objects.get().credential, credential)

    def test_create_without_credential_is_allowed(self):
        self.create_credential()
        self.client.post(self.url, self.data)
        self.assertIsNone(Project.objects.get().credential)

    def test_create_with_blank_credential_is_allowed(self):
        self.data["credential"] = ""
        self.client.post(self.url, self.data)
        self.assertIsNone(Project.objects.get().credential)

    def test_unknown_credential_is_rejected(self):
        self.assert_invalid_field("credential", 999)

    def test_invalid_form_preserves_selected_credential(self):
        credential = self.create_credential()
        self.data.update(credential=credential.pk, name="")
        response = self.client.post(self.url, self.data)
        option = f'<option value="{credential.pk}" selected>{credential.name}</option>'
        self.assertContains(response, option, html=True)
        self.assertFalse(Project.objects.exists())

    def test_edit_form_selects_saved_credential(self):
        project = self.create_project()
        project.credential = self.create_credential()
        project.save()
        response = self.client.get(reverse("edit_project", args=[project.pk]))
        self.assertEqual(
            response.context["form"]["credential"].value(), project.credential_id
        )

    def test_edit_changes_credential(self):
        project = self.create_project()
        project.credential = self.create_credential()
        project.save()
        credential = self.create_credential("Other account")
        self.data["credential"] = credential.pk
        response = self.client.post(
            reverse("edit_project", args=[project.pk]), self.data
        )
        self.assertRedirects(response, self.url)
        project.refresh_from_db()
        self.assertEqual(project.credential, credential)

    def test_edit_clears_credential(self):
        project = self.create_project()
        project.credential = self.create_credential()
        project.save()
        self.data["credential"] = ""
        response = self.client.post(
            reverse("edit_project", args=[project.pk]), self.data
        )
        self.assertRedirects(response, self.url)
        project.refresh_from_db()
        self.assertIsNone(project.credential)

    def test_creation_toggle_defaults_to_off(self):
        response = self.client.get(self.url)
        field = response.context["form"]["autonomous"]
        self.assertFalse(field.value())
        self.assertContains(response, 'type="checkbox" name="autonomous"')
        self.assertContains(
            response,
            "Import open issues and make imported issues eligible for claiming. Manual tasks remain independently claimable.",
        )

    def test_create_without_toggle_saves_manual_project(self):
        self.client.post(self.url, self.data)
        self.assertIs(Project.objects.get().autonomous, False)

    def test_create_with_toggle_saves_autonomous_project(self):
        self.data["autonomous"] = "on"
        response = self.client.post(self.url, self.data)
        self.assertRedirects(response, self.url)
        self.assertIs(Project.objects.get().autonomous, True)

    def test_invalid_form_preserves_autonomous_toggle(self):
        self.data.update(autonomous="on", name="")
        response = self.client.post(self.url, self.data)
        self.assertTrue(response.context["form"]["autonomous"].value())
        self.assertFalse(Project.objects.exists())

    def test_list_shows_autonomous_mode(self):
        project = self.create_project()
        project.autonomous = True
        project.save()
        response = self.client.get(self.url)
        self.assertContains(response, "Autonomous</span>")

    def test_list_shows_manual_mode(self):
        self.create_project()
        response = self.client.get(self.url)
        self.assertContains(response, "Manual</span>")

    def test_edit_form_contains_saved_autonomous_toggle(self):
        project = self.create_project()
        project.autonomous = True
        project.save()
        response = self.client.get(reverse("edit_project", args=[project.pk]))
        self.assertTrue(response.context["form"]["autonomous"].value())

    def test_edit_enables_autonomous_mode(self):
        project = self.create_project()
        self.data["autonomous"] = "on"
        self.client.post(reverse("edit_project", args=[project.pk]), self.data)
        project.refresh_from_db()
        self.assertIs(project.autonomous, True)

    def test_edit_disables_autonomous_mode(self):
        project = self.create_project()
        project.autonomous = True
        project.save()
        self.client.post(reverse("edit_project", args=[project.pk]), self.data)
        project.refresh_from_db()
        self.assertIs(project.autonomous, False)

    def test_blank_name_is_rejected(self):
        self.assert_invalid_field("name", "   ")

    def test_non_github_url_is_rejected(self):
        self.assert_invalid_field("repository_url", "https://example.com/owner/repo")

    def test_repository_subpage_is_rejected(self):
        url = "https://github.com/example/nightly/issues"
        self.assert_invalid_field("repository_url", url)

    def test_invalid_branch_is_rejected(self):
        self.assert_invalid_field("base_branch", "feature/../main")

    def test_invalid_form_keeps_entered_values(self):
        self.data["base_branch"] = "bad branch"
        response = self.client.post(self.url, self.data)
        self.assertContains(response, 'value="Nightly"')
        self.assertContains(response, 'value="bad branch"')

    def test_edit_form_contains_saved_details(self):
        project = self.create_project()
        response = self.client.get(reverse("edit_project", args=[project.pk]))
        self.assertContains(response, 'value="Nightly"')
        self.assertContains(response, 'value="develop"')
        self.assertContains(response, project.repository_url)

    def test_edit_updates_existing_project(self):
        project = self.create_project()
        self.data.update(
            name="Renamed",
            base_branch="",
            repository_url="https://github.com/example/new",
        )
        url = reverse("edit_project", args=[project.pk])
        response = self.client.post(url, self.data, follow=True)
        self.assertRedirects(response, self.url)
        project.refresh_from_db()
        self.assertEqual(project.name, "Renamed")
        self.assertEqual(project.repository_url, "https://github.com/example/new.git")
        self.assertEqual(project.base_branch, "")
        self.assertEqual(Project.objects.count(), 1)
        self.assertContains(response, "Project updated.")

    def test_invalid_edit_leaves_project_unchanged(self):
        project = self.create_project()
        self.data["base_branch"] = "bad branch"
        url = reverse("edit_project", args=[project.pk])
        response = self.client.post(url, self.data)
        self.assertIn("base_branch", response.context["form"].errors)
        project.refresh_from_db()
        self.assertEqual(project.base_branch, "develop")

    def test_edit_preserves_claimed_checkout(self):
        project = self.create_project()
        item = enqueue_item(project=project, title="Work")
        claim_next()
        self.data["repository_url"] = "https://github.com/example/new"
        self.client.post(reverse("edit_project", args=[project.pk]), self.data)
        item.refresh_from_db()
        self.assertEqual(item.project_id, project.pk)
        self.assertEqual(item.repository_url, project.repository_url)

    def test_missing_edit_returns_not_found(self):
        response = self.client.get(reverse("edit_project", args=[999]))
        self.assertEqual(response.status_code, 404)

    def test_create_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(self.url, self.data)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Project.objects.exists())

    def test_edit_requires_csrf(self):
        project = self.create_project()
        client = Client(enforce_csrf_checks=True)
        self.data["name"] = "Changed"
        url = reverse("edit_project", args=[project.pk])
        response = client.post(url, self.data)
        self.assertEqual(response.status_code, 403)
        project.refresh_from_db()
        self.assertEqual(project.name, "Nightly")

    def test_get_does_not_create_projects(self):
        self.client.get(self.url, self.data)
        self.assertFalse(Project.objects.exists())

    def test_unsupported_method_is_rejected(self):
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, 405)

    def test_project_name_is_escaped(self):
        self.data["name"] = "<script>alert('project')</script>"
        self.create_project()
        response = self.client.get(self.url)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, self.data["name"])

    def test_created_project_is_available_in_queue(self):
        self.client.post(self.url, self.data)
        response = self.client.get(reverse("index"))
        project = Project.objects.get()
        self.assertContains(
            response, f'<option value="{project.pk}">{project}</option>', html=True
        )

    def test_queue_links_to_projects(self):
        response = self.client.get(reverse("index"))
        self.assertContains(response, f'href="{self.url}"')
        self.assertNotContains(response, reverse("admin:projects_project_add"))
