import hashlib
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from projects.models import Project

from .github import GitHubAPIError, issue_data
from .models import QueueItem
from .services import (
    claim_item,
    claim_next,
    enqueue_item,
    import_issue,
    record_verified_merge,
    refresh_issue_for_launch,
    requeue_items,
    source_wait_reason,
)


def source_issue(number=1, **changes):
    now = timezone.now().isoformat()
    issue = dict(
        number=number,
        title=f"Issue {number}",
        body="Acceptance: tests pass",
        state="open",
        issue_id=1000 + number,
        author_id=42,
        author_login="owner",
        labels=[],
        created_at=now,
        updated_at=now,
        revision=hashlib.sha256(str(number).encode()).hexdigest(),
    )
    issue.update(changes)
    return issue


def source_repository(**changes):
    values = dict(
        repository_id=99,
        owner_id=42,
        repository_url="https://github.com/example/repo.git",
        default_branch="main",
    )
    values.update(changes)
    return values


def selected_project(**changes):
    values = dict(
        name="Selected",
        repository_url="https://github.com/example/repo",
        autonomous=True,
        github_repository_id=99,
        github_owner_id=42,
        github_default_branch="main",
        base_branch="main",
    )
    values.update(changes)
    return Project.objects.create(**values)


class WorkSelectionTests(TestCase):
    def setUp(self):
        repository = patch(
            "work_queue.github.repository_metadata", return_value=source_repository()
        )
        repository.start()
        self.addCleanup(repository.stop)
        comments = patch("work_queue.github.issue_comments", return_value=[])
        comments.start()
        self.addCleanup(comments.stop)
        self.project = selected_project()

    def issue(self, number=1, **changes):
        import_issue(project=self.project, **source_issue(number, **changes))
        return QueueItem.objects.get(project=self.project, issue_number=number)

    def merge(self, number):
        return record_verified_merge(
            repository_id=99,
            issue_number=number,
            pull_number=200 + number,
            merge_sha="a" * 40,
            merged_at=timezone.now(),
            provenance="github-pr-read",
            operation_id=f"merge-{number}",
        )

    def test_owner_issue_is_claimed_without_ready_label(self):
        item = self.issue()
        self.assertEqual(claim_next().pk, item.pk)

    def test_foreign_author_remains_visible_and_unclaimable(self):
        self.issue(author_id=43)
        self.assertIsNone(claim_next())
        self.assertContains(
            self.client.get(reverse("index")), "author is not permitted"
        )

    def test_explicit_author_ids_replace_owner_default(self):
        self.project.permitted_author_ids = [43]
        self.project.save()
        owner = self.issue()
        permitted = self.issue(2, author_id=43)
        self.assertEqual(claim_next().pk, permitted.pk)
        self.assertIsNone(claim_item(owner))

    def test_old_issue_waits_until_backfill_is_enabled(self):
        item = self.issue(created_at=(timezone.now() - timedelta(days=1)).isoformat())
        self.assertIn("Historical", source_wait_reason(item))
        self.project.backfill_issues = True
        self.project.save()
        self.assertEqual(claim_next().pk, item.pk)

    def test_incomplete_legacy_import_is_not_execution_permission(self):
        import_issue(project=self.project, number=1, title="Old", body="", state="open")
        self.assertIsNone(claim_next())

    def test_held_issue_cannot_be_claimed_with_stale_candidate(self):
        item = self.issue()
        self.issue(labels=["vibearr:hold"])
        self.assertIsNone(claim_item(item))

    def test_legacy_hold_label_blocks_work(self):
        self.issue(labels=["nq:hold"])
        self.assertIsNone(claim_next())

    def test_closed_dependency_does_not_satisfy_merge_requirement(self):
        self.issue(state="closed")
        dependent = self.issue(2, body="Depends on: #1")
        self.assertIn("verified merge", source_wait_reason(dependent))
        self.assertIsNone(claim_next())

    def test_verified_merge_unlocks_dependency(self):
        self.issue(state="closed")
        dependent = self.issue(2, body="Depends on: #1")
        self.merge(1)
        self.assertEqual(claim_next().pk, dependent.pk)

    def test_missing_dependency_has_visible_reason(self):
        self.assertIn(
            "Missing dependency #3",
            source_wait_reason(self.issue(body="Depends on: #3")),
        )

    def test_cross_repository_dependency_is_rejected(self):
        self.assertIn(
            "cross-repository",
            source_wait_reason(self.issue(body="Depends on: owner/repo#3")),
        )

    def test_malformed_dependency_is_rejected(self):
        self.assertIn(
            "Malformed", source_wait_reason(self.issue(body="Depends on: #1 and #2"))
        )

    def test_cycle_is_rejected(self):
        first = self.issue(body="Depends on: #2")
        self.issue(2, body="Depends on: #1")
        self.assertIn("Cyclic", source_wait_reason(first))

    def test_priority_labels_govern_claim_order(self):
        low = self.issue(labels=["vibearr:priority-low"])
        normal = self.issue(2)
        high = self.issue(3, labels=["vibearr:priority-high"])
        self.assertEqual(
            [claim_next().pk for _ in range(3)], [high.pk, normal.pk, low.pk]
        )

    def test_branch_template_is_frozen_and_retained_on_retry(self):
        item = self.issue(body="Branch: feat/example")
        claimed = claim_next()
        self.assertEqual(claimed.branch_name, "feat/example")
        self.issue(body="Branch: feat/changed")
        requeue_items(QueueItem.objects.filter(pk=item.pk))
        self.assertEqual(claim_next().branch_name, "feat/example")

    def test_invalid_branch_is_not_claimed(self):
        self.issue(body="Branch: bad..branch")
        self.assertIsNone(claim_next())

    def test_base_branch_cannot_be_claimed(self):
        self.project.base_branch = "main"
        self.project.save()
        self.issue(body="Branch: main")
        self.assertIsNone(claim_next())

    def test_branch_collision_prevents_second_claim(self):
        first = self.issue(body="Branch: feat/shared")
        self.issue(2, body="Branch: feat/shared")
        self.assertEqual(claim_next().pk, first.pk)
        self.assertIsNone(claim_next())

    def test_fallbacks_are_distinct_for_manual_and_imported_work(self):
        imported = self.issue()
        manual = enqueue_item(project=self.project, title="Manual")
        self.assertEqual(
            claim_next().branch_name, f"vibearr/issue-{imported.issue_number}"
        )
        self.assertEqual(claim_next().branch_name, f"vibearr/manual-{manual.pk}")

    def test_running_snapshot_does_not_change_on_sync(self):
        self.issue()
        item = claim_next()
        snapshot = item.source_snapshot
        self.issue(body="New instructions", title="Edited")
        item.refresh_from_db()
        self.assertEqual(item.source_snapshot, snapshot)
        self.assertEqual(item.instructions, "Acceptance: tests pass")

    @patch("work_queue.github.read_issue", side_effect=GitHubAPIError("offline"))
    def test_fresh_read_failure_blocks_launch(self, read):
        self.issue()
        item = claim_next()
        with self.assertRaises(GitHubAPIError):
            refresh_issue_for_launch(
                item_id=item.pk, claim_token=item.claim_token, token="test"
            )

    def test_stale_claim_cannot_refresh_context(self):
        self.issue()
        item = claim_next()
        with self.assertRaises(ValidationError):
            refresh_issue_for_launch(item_id=item.pk, claim_token="stale", token="test")

    def test_changed_source_blocks_launch_without_rewriting_snapshot(self):
        self.issue()
        item = claim_next()
        with patch(
            "work_queue.github.read_issue", return_value=source_issue(revision="f" * 64)
        ):
            with self.assertRaisesMessage(ValidationError, "changed after claim"):
                refresh_issue_for_launch(
                    item_id=item.pk, claim_token=item.claim_token, token="test"
                )
        item.refresh_from_db()
        self.assertNotEqual(item.source_snapshot["revision"], "f" * 64)

    def test_merge_operation_is_idempotent_and_conflicts_are_rejected(self):
        now = timezone.now()
        values = dict(
            repository_id=99,
            issue_number=1,
            pull_number=201,
            merge_sha="a" * 40,
            merged_at=now,
            provenance="github-pr-read",
            operation_id="op",
        )
        self.assertEqual(
            record_verified_merge(**values).pk, record_verified_merge(**values).pk
        )
        with self.assertRaises(ValidationError):
            record_verified_merge(**{**values, "merge_sha": "b" * 40})

    @override_settings(NQ_API_TOKEN="test")
    def test_generic_api_cannot_bypass_hold(self):
        self.issue(labels=["vibearr:hold"])
        response = self.client.post(
            reverse("api_claim"),
            "{}",
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer test",
        )
        self.assertIsNone(response.json()["item"])

    def test_reopening_completed_issue_does_not_requeue(self):
        item = self.issue()
        QueueItem.objects.filter(pk=item.pk).update(status="succeeded")
        self.issue(state="closed")
        self.issue(state="open")
        self.assertIsNone(claim_next())

    def test_stale_closure_does_not_remove_newer_open_issue_from_queue(self):
        now = timezone.now()
        item = self.issue(updated_at=now.isoformat())
        self.issue(state="closed", updated_at=(now - timedelta(seconds=1)).isoformat())
        item.refresh_from_db()
        self.assertEqual(item.status, QueueItem.Status.QUEUED)
        self.assertEqual(item.issue_state, QueueItem.IssueState.OPEN)

    def test_stale_reopening_does_not_restore_closed_issue_to_queue(self):
        now = timezone.now()
        item = self.issue(state="closed", updated_at=now.isoformat())
        self.issue(updated_at=(now - timedelta(seconds=1)).isoformat())
        item.refresh_from_db()
        self.assertEqual(item.status, QueueItem.Status.CLOSED)
        self.assertEqual(item.issue_state, QueueItem.IssueState.CLOSED)

    def test_expired_sync_lease_cannot_mutate_source(self):
        import uuid

        item = self.issue()
        lease = uuid.uuid4()
        Project.objects.filter(pk=self.project.pk).update(
            sync_lock_token=lease, sync_lock_until=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaisesMessage(ValidationError, "lease expired"):
            import_issue(
                project=self.project,
                sync_lock_token=lease,
                **source_issue(title="Stale importer"),
            )
        item.refresh_from_db()
        self.assertEqual(item.title, "Issue 1")

    def test_imported_branch_cannot_reserve_manual_fallback_or_block_queue(self):
        manual = enqueue_item(project=self.project, title="Manual first")
        self.issue(
            body=f"Branch: vibearr/manual-{manual.pk}", labels=["vibearr:priority-high"]
        )
        later = enqueue_item(project=self.project, title="Manual second")
        self.assertEqual(claim_next().pk, manual.pk)
        self.assertEqual(claim_next().pk, later.pk)
        self.assertIsNone(claim_next())

    def test_stale_provider_reply_cannot_authorize_saved_newer_revision(self):
        original = source_issue()
        import_issue(project=self.project, **original)
        claimed = claim_next()
        updated = {
            **original,
            "revision": "b" * 64,
            "body": "New instructions",
            "updated_at": (timezone.now() + timedelta(seconds=1)).isoformat(),
        }
        import_issue(project=self.project, **updated)
        with patch("work_queue.github.read_issue", return_value=original):
            with self.assertRaisesMessage(ValidationError, "changed after claim"):
                refresh_issue_for_launch(
                    item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
                )
        claimed.refresh_from_db()
        self.assertEqual(claimed.issue_revision, "b" * 64)
        self.assertEqual(claimed.source_snapshot["revision"], original["revision"])

    def test_foreign_directive_cannot_reserve_owner_fallback(self):
        self.issue(1, author_id=1337, body="Branch: vibearr/issue-100")
        owner = self.issue(100)
        self.assertEqual(claim_next().pk, owner.pk)
        self.assertIsNone(claim_next())

    def test_held_directive_cannot_reserve_another_issue_branch(self):
        self.issue(1, labels=["vibearr:hold"], body="Branch: feat/owner")
        owner = self.issue(100, body="Branch: feat/owner")
        self.assertEqual(claim_next().pk, owner.pk)

    def test_unclaimed_directive_cannot_reserve_manual_branch(self):
        manual = enqueue_item(project=self.project, title="Manual")
        self.issue(author_id=1337, body=f"Branch: vibearr/manual-{manual.pk}")
        self.assertEqual(claim_next().pk, manual.pk)

    def test_explicit_alternate_base_also_protects_remote_default(self):
        self.project.base_branch = "release"
        self.project.save()
        item = self.issue(body="Branch: main")
        self.assertIn("base branch", source_wait_reason(item))
        self.assertIsNone(claim_next())

    def test_fresh_default_change_blocks_already_claimed_task_branch(self):
        self.project.base_branch = "release"
        self.project.save()
        original = source_issue(body="Branch: feat/candidate")
        import_issue(project=self.project, **original)
        claimed = claim_next()
        with (
            patch(
                "work_queue.github.repository_metadata",
                return_value=source_repository(default_branch="feat/candidate"),
            ),
            patch("work_queue.github.read_issue", return_value=original),
        ):
            with self.assertRaisesMessage(ValidationError, "base branch"):
                refresh_issue_for_launch(
                    item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
                )
        claimed.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(claimed.base_branch, "release")
        self.assertEqual(claimed.branch_name, "feat/candidate")
        self.assertEqual(self.project.github_default_branch, "feat/candidate")
        self.assertNotIn("comments", claimed.source_snapshot)

    def test_fresh_owner_transfer_revokes_default_former_owner(self):
        original = source_issue()
        import_issue(project=self.project, **original)
        claimed = claim_next()
        with (
            patch(
                "work_queue.github.repository_metadata",
                return_value=source_repository(owner_id=777),
            ),
            patch("work_queue.github.read_issue", return_value=original),
        ):
            with self.assertRaisesMessage(ValidationError, "author is not permitted"):
                refresh_issue_for_launch(
                    item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
                )
        self.project.refresh_from_db()
        claimed.refresh_from_db()
        self.assertEqual(self.project.github_owner_id, 777)
        self.assertNotIn("comments", claimed.source_snapshot)

    def test_transfer_preserves_explicit_author_permission(self):
        self.project.permitted_author_ids = [42]
        self.project.save()
        original = source_issue()
        import_issue(project=self.project, **original)
        claimed = claim_next()
        with (
            patch(
                "work_queue.github.repository_metadata",
                return_value=source_repository(owner_id=777),
            ),
            patch("work_queue.github.read_issue", return_value=original),
        ):
            snapshot = refresh_issue_for_launch(
                item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
            )
        self.project.refresh_from_db()
        self.assertEqual(self.project.github_owner_id, 777)
        self.assertEqual(snapshot["metadata"]["author_id"], 42)

    def test_source_refresh_fences_project_reassignment_during_fetch(self):
        original = source_issue()
        import_issue(project=self.project, **original)
        claimed = claim_next()
        replacement = selected_project(name="Replacement")
        expected = {}

        def provider(project, number, token, **kwargs):
            QueueItem.objects.filter(pk=claimed.pk).update(project=replacement)
            expected.update(QueueItem.objects.values().get(pk=claimed.pk))
            return {**original, "state": "closed", "revision": "c" * 64}

        with patch("work_queue.github.read_issue", side_effect=provider):
            with self.assertRaisesMessage(ValidationError, "Project changed"):
                refresh_issue_for_launch(
                    item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
                )
        self.assertEqual(QueueItem.objects.values().get(pk=claimed.pk), expected)

    def assert_stale_repository_reply_preserves_newer_authority(self, newer):
        original = source_issue()
        import_issue(project=self.project, **original)
        claimed = claim_next()
        expected = QueueItem.objects.values().get(pk=claimed.pk)
        requests = []

        def repository(*args, **kwargs):
            requests.append(True)
            if len(requests) == 2:
                Project.objects.filter(pk=self.project.pk).update(**newer)
            return source_repository()

        with (
            patch("work_queue.github.repository_metadata", side_effect=repository),
            patch("work_queue.github.read_issue", return_value=original),
        ):
            with self.assertRaisesMessage(
                ValidationError, "Repository authority changed"
            ):
                refresh_issue_for_launch(
                    item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
                )
        self.project.refresh_from_db()
        for field, value in newer.items():
            self.assertEqual(getattr(self.project, field), value)
        self.assertEqual(QueueItem.objects.values().get(pk=claimed.pk), expected)

    def test_stale_owner_reply_cannot_restore_former_authority(self):
        self.assert_stale_repository_reply_preserves_newer_authority(
            {"github_owner_id": 777}
        )

    def test_stale_default_reply_cannot_unprotect_new_default(self):
        self.assert_stale_repository_reply_preserves_newer_authority(
            {"github_default_branch": "vibearr/issue-1"}
        )

    def test_stale_canonical_url_cannot_override_accepted_rename(self):
        self.assert_stale_repository_reply_preserves_newer_authority(
            {"repository_url": "https://github.com/new-owner/repo.git"}
        )

    def test_newer_identical_observation_fences_older_provider_reply(self):
        from .services import accept_repository_observation, repository_authority_state

        original = source_issue()
        import_issue(project=self.project, **original)
        claimed = claim_next()
        requests = []

        def repository(*args, **kwargs):
            requests.append(True)
            if len(requests) == 2:
                current = Project.objects.get(pk=self.project.pk)
                accept_repository_observation(
                    project=current,
                    repository=source_repository(),
                    expected=repository_authority_state(current),
                )
            return source_repository()

        with (
            patch("work_queue.github.repository_metadata", side_effect=repository),
            patch("work_queue.github.read_issue", return_value=original),
        ):
            with self.assertRaisesMessage(
                ValidationError, "Repository authority changed"
            ):
                refresh_issue_for_launch(
                    item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
                )
        self.project.refresh_from_db()
        self.assertEqual(self.project.github_authority_version, 1)
        self.assertEqual(self.project.github_owner_id, 42)


class SourceMetadataTests(TestCase):
    def test_incomplete_provider_metadata_is_rejected(self):
        with self.assertRaises(GitHubAPIError):
            issue_data({"number": 1, "title": "Issue", "body": "", "state": "open"})


class FreshRepositoryAuthorityTests(TestCase):
    def test_real_repository_response_transfer_is_applied_before_permission(self):
        project = selected_project()
        now = timezone.now().isoformat()
        raw = dict(
            number=1,
            id=1001,
            title="Owner work",
            body="Acceptance: pass",
            state="open",
            user={"id": 42, "login": "former-owner"},
            labels=[],
            created_at=now,
            updated_at=now,
        )
        import_issue(project=project, **issue_data(raw))
        claimed = claim_next()
        repository = dict(
            id=99,
            owner={"id": 777},
            html_url="https://github.com/new-owner/repo",
            default_branch="trunk",
        )

        def provider(path, token, etag=""):
            if path == "/repositories/99":
                return repository, {}
            if "/comments?" in path:
                return [], {}
            self.assertEqual(path, "/repos/new-owner/repo/issues/1")
            return raw, {}

        with patch("work_queue.github.github_read", side_effect=provider):
            with self.assertRaisesMessage(ValidationError, "author is not permitted"):
                refresh_issue_for_launch(
                    item_id=claimed.pk, claim_token=claimed.claim_token, token="test"
                )
        project.refresh_from_db()
        self.assertEqual(project.github_owner_id, 777)
        self.assertEqual(project.github_default_branch, "trunk")
        self.assertEqual(
            project.repository_url, "https://github.com/new-owner/repo.git"
        )
