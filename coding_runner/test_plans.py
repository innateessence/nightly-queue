import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from .context import ContextError, canonical_bytes, digest, manual_context
from .plan_store import ContextPlanStore
from .plans import register_context_sources, validate_plan
from .test_context import ITEM
from .workspaces import SourceStore

BASE = "a" * 40


def saved_context(item=None):
    item = item or {**ITEM, "source": "manual"}
    document = manual_context(item)
    return {
        "id": str(uuid4()),
        "document": document,
        "digest": digest(document),
        "inspected_base_commit": BASE,
        "resolved_base_branch": "main",
    }


def proposed_plan(context):
    source = {"kind": "repository", "reference": "README.md", "digest": "b" * 64}
    action = {
        "argv": ["/usr/bin/python3", "-m", "unittest"],
        "mode": "exec",
        "source_kind": source["kind"],
        "source_reference": source["reference"],
        "source_digest": source["digest"],
        "workspace_id": str(uuid4()),
        "base_revision": context["inspected_base_commit"],
        "content_digest": "c" * 64,
    }
    return {
        "schema_version": 1,
        "context_id": context["id"],
        "context_digest": context["digest"],
        "problem": "Preserve task state across retries",
        "expected_behavior": "Retries reuse saved state",
        "acceptance_criteria": ["A restart retains the original task"],
        "findings": ["Existing state is durable"],
        "sources": [source],
        "relevant_paths": ["README.md"],
        "approach": "Record the checkpoint before advancing",
        "tradeoffs": ["Extra durable writes"],
        "steps": ["Add the checkpoint", "Verify restart behavior"],
        "file_changes": ["README.md"],
        "setup_actions": [],
        "validation_actions": [action],
        "regression_cases": ["Restart after upload"],
        "delivery_evidence": ["Passing restart test and inspected diff"],
        "blockers": [],
        "open_questions": [],
        "original_issue": (
            context["document"]["issue"]["url"]
            if context["document"]["issue"]
            else None
        ),
        "checkout": context["document"]["checkout"],
        "resolved_base_branch": "main",
        "inspected_base_commit": context["inspected_base_commit"],
    }


class PlanContractTests(unittest.TestCase):
    def setUp(self):
        self.context = saved_context()
        self.plan = proposed_plan(self.context)

    def validate(self, plan):
        return validate_plan(plan, self.context["document"], self.context["id"], BASE)

    def test_complete_plan_accepts_structured_unapproved_actions(self):
        self.assertEqual(self.validate(self.plan), self.plan)
        self.assertNotIn("approved", self.plan)

    def test_missing_acceptance_criteria_is_rejected(self):
        self.plan.pop("acceptance_criteria")
        with self.assertRaises(ContextError):
            self.validate(self.plan)

    def test_unstructured_validation_command_is_rejected(self):
        self.plan["validation_actions"] = ["python -m unittest"]
        with self.assertRaises(ContextError):
            self.validate(self.plan)

    def test_changed_action_source_binding_is_rejected(self):
        self.plan["validation_actions"][0]["source_digest"] = "d" * 64
        with self.assertRaises(ContextError):
            self.validate(self.plan)

    def test_fake_backend_approval_is_rejected(self):
        self.plan["validation_actions"][0]["approved"] = True
        with self.assertRaises(ContextError):
            self.validate(self.plan)

    def test_changed_checkout_is_rejected(self):
        self.plan["checkout"] = {
            **self.plan["checkout"],
            "repository_url": "https://github.com/changed/project",
        }
        with self.assertRaises(ContextError):
            self.validate(self.plan)

    def test_path_escape_is_rejected(self):
        self.plan["file_changes"] = ["../credentials"]
        with self.assertRaises(ContextError):
            self.validate(self.plan)

    def test_blockers_are_saved_as_data(self):
        self.plan["blockers"] = [
            "Need clarification of conflicting acceptance criteria"
        ]
        self.assertEqual(self.validate(self.plan)["blockers"], self.plan["blockers"])

    def test_source_store_digest_is_computed_from_actual_supervisor_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SourceStore(Path(directory) / "sources")
            refs = register_context_sources(self.context["document"], store)
            self.assertEqual(
                refs[0]["digest"], store.current_digest("operator", "manual-task")
            )
            import hashlib

            self.assertEqual(
                refs[0]["digest"],
                hashlib.sha256(
                    canonical_bytes(
                        {
                            key: value
                            for key, value in self.context["document"].items()
                            if key != "fetched_at"
                        }
                    )
                ).hexdigest(),
            )

    def test_handoff_refresh_failure_does_not_request_handoff(self):
        client = Mock()
        store = ContextPlanStore(client, str(uuid4()), str(uuid4()), Mock())
        issue_client = Mock()
        issue_client.fetch.side_effect = ContextError("Unavailable discussion")
        with self.assertRaises(ContextError):
            store.handoff(ITEM, BASE, issue_client=issue_client)
        client.request.assert_not_called()

    def test_manual_refresh_uses_supervisor_document_and_resolved_branch(self):
        client = Mock()
        client.request.return_value = {"context": self.context}
        source_store = Mock()
        store = ContextPlanStore(client, str(uuid4()), str(uuid4()), source_store)
        actual = store.refresh({**ITEM, "source": "manual"}, BASE)
        self.assertEqual(actual["id"], self.context["id"])
        self.assertEqual(
            client.request.call_args.args[2]["resolved_base_branch"], "main"
        )
        source_store.register.assert_called_once()

    def test_blank_requested_base_needs_resolved_default(self):
        client = Mock()
        store = ContextPlanStore(client, str(uuid4()), str(uuid4()), Mock())
        with self.assertRaisesRegex(ValueError, "remote default"):
            store.refresh({**ITEM, "source": "manual", "base_branch": ""}, BASE)
        client.request.assert_not_called()

    def test_saved_context_digest_is_verified(self):
        client = Mock()
        invalid = copy.deepcopy(self.context)
        invalid["document"]["title"] = "Tampered saved title"
        client.request.return_value = {"context": invalid}
        store = ContextPlanStore(client, str(uuid4()), str(uuid4()), Mock())
        with self.assertRaisesRegex(ValueError, "digest"):
            store.refresh({**ITEM, "source": "manual"}, BASE)

    def test_removed_comment_invalidates_old_source_digest_after_restart(self):
        from .context import GitHubContextClient
        from .test_context import comment, issue
        from unittest.mock import patch

        reader = GitHubContextClient("selected-token")
        with patch.object(
            reader, "_get", side_effect=[(issue(comments=1), ""), ([comment()], "")]
        ):
            initial = reader.fetch(ITEM)
        with patch.object(reader, "_get", side_effect=[(issue(), ""), ([], "")]):
            current = reader.fetch(ITEM)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sources"
            first = register_context_sources(initial, SourceStore(path))
            original = next(source for source in first if source["kind"] == "comment")
            register_context_sources(current, SourceStore(path))
            changed = SourceStore(path).current_digest(
                original["kind"], original["reference"]
            )
            self.assertNotEqual(original["digest"], changed)

    def test_unchanged_manual_refresh_keeps_authoritative_source_digest(self):
        first = self.context["document"]
        second = {**first, "fetched_at": "2026-11-16T12:00:00Z"}
        with tempfile.TemporaryDirectory() as directory:
            store = SourceStore(Path(directory) / "sources")
            self.assertEqual(
                register_context_sources(first, store),
                register_context_sources(second, store),
            )

    def test_handoff_retry_recovers_original_operation_without_another_refresh(self):
        operation = str(uuid4())
        response = {
            "plan": self.plan,
            "context": self.context,
            "actions_require_approval": True,
        }
        client = Mock()
        client.request.return_value = response
        issue_client = Mock()
        store = ContextPlanStore(client, str(uuid4()), str(uuid4()), Mock())
        self.assertEqual(
            store.handoff(
                {**ITEM, "source": "manual"},
                BASE,
                issue_client=issue_client,
                operation_id=operation,
            ),
            response,
        )
        issue_client.fetch.assert_not_called()
        self.assertEqual(
            client.request.call_args.args,
            ("GET", f"/api/v1/runner/runs/{store.run_id}/handoff/{operation}/"),
        )

    def test_new_handoff_operation_refreshes_before_posting(self):
        from .client import NotFoundError

        operation = str(uuid4())
        response = {"plan": self.plan, "context": self.context}
        client = Mock()
        client.request.side_effect = [
            NotFoundError(),
            {"context": self.context},
            response,
        ]
        store = ContextPlanStore(client, str(uuid4()), str(uuid4()), Mock())
        self.assertEqual(
            store.handoff({**ITEM, "source": "manual"}, BASE, operation_id=operation),
            response,
        )
        self.assertEqual(
            [call.args[0] for call in client.request.call_args_list],
            ["GET", "POST", "POST"],
        )

    def test_issue_title_change_invalidates_original_issue_provenance(self):
        from .context import GitHubContextClient
        from .test_context import issue
        from unittest.mock import patch

        reader = GitHubContextClient("selected-token")
        with patch.object(reader, "_get", side_effect=[(issue(), ""), ([], "")]):
            original = reader.fetch(ITEM)
        changed = {**original, "title": "Run a different command"}
        with tempfile.TemporaryDirectory() as directory:
            store = SourceStore(Path(directory) / "sources")
            first = register_context_sources(original, store)[0]
            second = register_context_sources(changed, SourceStore(store.directory))[0]
            self.assertNotEqual(first["digest"], second["digest"])
