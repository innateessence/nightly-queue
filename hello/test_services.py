from django.test import TestCase

from .models import Project, QueueItem
from .services import claim_item, claim_next, enqueue_item, finish_item, requeue_items


class QueueServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Example",
            repository_url="https://github.com/example/repo",
            base_branch="develop",
        )
        self.item = enqueue_item(project=self.project, title="Work")
        self.items = QueueItem.objects.filter(pk=self.item.pk)

    def test_claim_rejects_candidate_claimed_by_another_worker(self):
        candidate = QueueItem.objects.get(pk=self.item.pk)
        first = claim_item(candidate)
        self.assertIsNotNone(first)
        self.assertIsNone(claim_item(candidate))
        self.item.refresh_from_db()
        self.assertEqual(self.item.claim_token, first.claim_token)

    def test_claim_reloads_reassigned_item_before_capturing_checkout(self):
        items = QueueItem.objects.select_related("project")
        candidate = items.get(pk=self.item.pk)
        replacement = Project.objects.create(
            name="Replacement",
            repository_url="https://github.com/example/replacement",
            base_branch="release",
        )
        self.items.update(project=replacement)

        self.assertIsNone(claim_item(candidate))
        claimed = claim_next()
        self.assertEqual(claimed.project_id, replacement.pk)
        self.assertEqual(claimed.repository_url, replacement.repository_url)
        self.assertEqual(claimed.base_branch, "release")

    def test_claiming_remaining_item_does_not_reuse_running_work(self):
        second = enqueue_item(project=self.project, title="Second")
        first_claim = claim_next()
        second_claim = claim_next()
        self.assertEqual(first_claim.pk, self.item.pk)
        self.assertEqual(second_claim.pk, second.pk)
        self.assertNotEqual(first_claim.claim_token, second_claim.claim_token)

    def test_requeue_invalidates_previous_claim(self):
        first = claim_next()
        requeue_items(self.items)
        second = claim_next()
        updated = finish_item(
            item_id=self.item.pk, claim_token=first.claim_token, status="succeeded"
        )
        self.assertEqual(updated, 0)
        self.assertNotEqual(first.claim_token, second.claim_token)

    def test_new_claim_can_complete_requeued_work(self):
        claim_next()
        requeue_items(self.items)
        second = claim_next()
        updated = finish_item(
            item_id=self.item.pk, claim_token=second.claim_token, status="succeeded"
        )
        self.assertEqual(updated, 1)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "succeeded")

    def test_retry_preserves_repository_after_project_edit(self):
        first = claim_next()
        requeue_items(self.items)
        self.project.repository_url = "https://github.com/example/renamed"
        self.project.save()
        second = claim_next()
        self.assertEqual(second.repository_url, first.repository_url)

    def test_retry_preserves_base_branch_after_project_edit(self):
        claim_next()
        requeue_items(self.items)
        self.project.base_branch = "main"
        self.project.save()
        second = claim_next()
        self.assertEqual(second.base_branch, "develop")

    def test_retry_preserves_feature_branch(self):
        first = claim_next()
        requeue_items(self.items)
        second = claim_next()
        self.assertEqual(second.branch_name, first.branch_name)

    def test_retry_preserves_unspecified_base_branch(self):
        self.project.base_branch = ""
        self.project.save()
        claim_next()
        requeue_items(self.items)
        self.project.base_branch = "main"
        self.project.save()
        self.assertEqual(claim_next().base_branch, "")

    def test_requeue_clears_attempt_state(self):
        first = claim_next(execution_ref="n8n:123")
        finish_item(
            item_id=first.pk,
            claim_token=first.claim_token,
            status="failed",
            result_url="https://example.com/result",
            error_summary="Agent stopped",
        )
        requeue_items(self.items)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "queued")
        self.assertIsNone(self.item.claim_token)
        self.assertIsNone(self.item.started_at)
        self.assertIsNone(self.item.finished_at)
        self.assertEqual(self.item.execution_ref, "")
        self.assertEqual(self.item.result_url, "")
        self.assertEqual(self.item.error_summary, "")

    def test_requeue_leaves_completed_work_alone(self):
        first = claim_next()
        finish_item(item_id=first.pk, claim_token=first.claim_token, status="succeeded")
        self.assertEqual(requeue_items(self.items), 0)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, "succeeded")
