import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from .context import ContextError, canonical_bytes, digest, manual_context

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
