from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from .models import Project
from .services import claim_next, enqueue_item


class RequeueActionTests(TestCase):
    def setUp(self):
        url = "https://github.com/example/repo"
        project = Project.objects.create(name="Example", repository_url=url)
        enqueue_item(project=project, title="Work")
        self.item = claim_next()
        users = get_user_model().objects
        self.user = users.create_user(username="viewer", is_staff=True)
        permissions = Permission.objects.filter(content_type__app_label="hello")
        permission = permissions.get(codename="view_queueitem")
        self.user.user_permissions.add(permission)
        self.client.force_login(self.user)
        self.url = reverse("admin:hello_queueitem_changelist")

    def post_requeue(self):
        data = {
            "action": "requeue_stopped_work",
            "index": "0",
            "_selected_action": [str(self.item.pk)],
        }
        return self.client.post(self.url, data)

    def test_view_only_staff_cannot_see_requeue_action(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "requeue_stopped_work")

    def test_view_only_staff_cannot_submit_requeue_action(self):
        token = self.item.claim_token
        self.post_requeue()
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "running")
        self.assertEqual(self.item.claim_token, token)

    def test_staff_with_change_permission_can_requeue(self):
        permissions = Permission.objects.filter(content_type__app_label="hello")
        permission = permissions.get(codename="change_queueitem")
        self.user.user_permissions.add(permission)
        response = self.post_requeue()
        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertIsNone(self.item.claim_token)
