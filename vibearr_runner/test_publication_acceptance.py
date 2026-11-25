from datetime import datetime, timedelta, timezone
from pathlib import Path
import importlib.util
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from vibearr_runner.bootstrap_files import private_json
from vibearr_runner import publication_acceptance


class AcceptanceTests(unittest.TestCase):
    def test_protected_epoch_receipt_time_matches_server_iso_time(self):
        epoch = 1794852047.8716285
        iso = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        self.assertEqual(
            publication_acceptance.timestamp(epoch),
            publication_acceptance.timestamp(iso),
        )
        for invalid in (True, None, float("nan"), float("inf"), -1):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                publication_acceptance.timestamp(invalid)

    def test_evidence_history_fetches_all_pages(self):
        queue = Mock()
        queue.request.side_effect = [
            {"evidence": [{"id": 1}], "has_more": True, "next_after": 1},
            {"evidence": [{"id": 2}], "has_more": False, "next_after": None},
        ]
        result = publication_acceptance.evidence_entries(queue, str(uuid4()))
        self.assertEqual(result, [{"id": 1}, {"id": 2}])
        self.assertEqual(queue.request.call_args.kwargs["query"], {"after": 1})

    def test_evidence_nonadvancing_cursor_is_rejected(self):
        queue = Mock()
        queue.request.return_value = {
            "evidence": [{"id": 1}],
            "has_more": True,
            "next_after": 0,
        }
        with self.assertRaisesRegex(ValueError, "cursor"):
            publication_acceptance.evidence_entries(queue, str(uuid4()))

    def test_private_operator_file_rejects_world_readable_secret_and_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "operator.json"
            path.write_text("{}")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                private_json(path)
            path.chmod(0o600)
            link = Path(temporary) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                private_json(link)

    def test_restart_receipt_requires_same_agent_worker_boot(self):
        identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "b" * 64,
        }
        record = {
            "run_id": "run",
            "agent": {
                "receipt": {"operation_id": "agent", "execution_identity": identity}
            },
        }
        operation = {
            "operation_id": "agent",
            "execution_identity": identity,
            "run_id": "run",
            "state": "running",
            "phase": "agent",
            "pid": 1,
        }
        other = {
            **operation,
            "pid": 2,
            "execution_identity": {**identity, "worker_boot": str(uuid4())},
        }
        receipt = {"executor_observations": {"agent": [operation, other]}}
        with self.assertRaisesRegex(ValueError, "worker identity"):
            publication_acceptance.restart_evidence(record, receipt)

    def test_restart_requires_real_validation_observation(self):
        identity = {
            "runtime": "compose-slot-v2",
            "protocol_version": 2,
            "instance_id": str(uuid4()),
            "worker_boot": str(uuid4()),
            "binding_digest": "b" * 64,
        }
        record = {
            "run_id": "run",
            "agent": {
                "receipt": {"operation_id": "agent", "execution_identity": identity}
            },
            "validation": {
                "checks": [
                    {
                        "receipt": {
                            "operation_id": "validation",
                            "execution_identity": identity,
                        }
                    }
                ]
            },
        }
        operation = {
            "operation_id": "agent",
            "execution_identity": identity,
            "run_id": "run",
            "state": "running",
            "phase": "agent",
            "pid": 1,
        }
        receipt = {
            "executor_observations": {"agent": [operation, {**operation, "pid": 2}]}
        }
        with self.assertRaisesRegex(ValueError, "independent validation"):
            publication_acceptance.restart_evidence(record, receipt)


class ProviderObservationTests(unittest.TestCase):
    def setUp(self):
        self.workspace = {
            "repository_id": 99,
            "repository": "owner/repo",
            "branch": "acceptance/task",
            "base_branch": "main",
            "base_revision": "b" * 40,
            "commit_sha": "a" * 40,
        }
        self.delivery = {
            "workspace": self.workspace,
            "delivery": {"pull_request": {"number": 7}},
        }
        created = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        repository = {"id": 99, "full_name": "owner/repo"}
        self.pull = {
            "number": 7,
            "state": "open",
            "draft": False,
            "merged": False,
            "html_url": "https://github.com/owner/repo/pull/7",
            "created_at": created,
            "head": {"sha": "a" * 40, "ref": "acceptance/task", "repo": repository},
            "base": {"ref": "main", "repo": repository},
        }
        self.github = Mock()
        self.github.pull.return_value = self.pull

    def snapshot(self):
        return {
            "delivery": self.delivery,
            "provider_delivery_snapshot": {
                "observed_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
                "head_sha": "a" * 40,
                "base": "main",
                "number": 7,
                "repository_id": 99,
            },
        }

    def test_first_collection_requires_the_actual_tested_head(self):
        self.pull["head"]["sha"] = "c" * 40
        with self.assertRaisesRegex(ValueError, "before an exact-head"):
            publication_acceptance.observe_pull(self.github, self.delivery)

    def test_post_delivery_descendant_keeps_original_validation_and_marks_current_head_unverified(
        self,
    ):
        self.pull["head"]["sha"] = "c" * 40
        self.pull.update(
            state="closed",
            merged=True,
            merge_commit_sha="d" * 40,
            merged_at=datetime.now(timezone.utc).isoformat(),
        )
        self.github.request.return_value = {
            "status": "ahead",
            "merge_base_commit": {"sha": "a" * 40},
        }
        _, original, current = publication_acceptance.observe_pull(
            self.github, self.delivery, self.snapshot()
        )
        self.assertEqual(original["head_sha"], "a" * 40)
        self.assertEqual(current["head_sha"], "c" * 40)
        self.assertEqual(current["native_validation_applies_to_sha"], "a" * 40)
        self.assertFalse(current["current_head_native_validated"])
        self.assertEqual(current["review_status"], "not-verified-by-this-driver")
        self.assertEqual(current["provider_merge"]["commit_sha"], "d" * 40)
        self.assertEqual(self.delivery["workspace"]["commit_sha"], "a" * 40)

    def test_post_delivery_head_without_original_ancestry_is_rejected(self):
        self.pull["head"]["sha"] = "c" * 40
        self.github.request.return_value = {
            "status": "ahead",
            "merge_base_commit": {"sha": "e" * 40},
        }
        with self.assertRaisesRegex(ValueError, "ancestor"):
            publication_acceptance.observe_pull(
                self.github, self.delivery, self.snapshot()
            )

    def test_saved_observation_cannot_replace_immutable_server_delivery(self):
        snapshot = self.snapshot()
        snapshot["delivery"] = {"different": "receipt"}
        with self.assertRaisesRegex(ValueError, "Immutable server delivery"):
            publication_acceptance.observe_pull(self.github, self.delivery, snapshot)

    def test_incomplete_saved_provider_observation_is_not_proof(self):
        snapshot = self.snapshot()
        snapshot["provider_delivery_snapshot"].pop("observed_at")
        with self.assertRaisesRegex(ValueError, "observation time"):
            publication_acceptance.observe_pull(self.github, self.delivery, snapshot)

    def test_branch_query_does_not_interpret_task_text_as_query_parameters(self):
        from urllib.parse import parse_qs, urlsplit

        publication_acceptance.pulls_for_branch(
            self.github, "owner/repo", "acceptance/task&state=closed"
        )
        query = parse_qs(urlsplit(self.github.request.call_args.args[1]).query)
        self.assertEqual(query["state"], ["all"])
        self.assertEqual(query["head"], ["owner:acceptance/task&state=closed"])


@unittest.skipUnless(
    importlib.util.find_spec("vibearr_executor"),
    "Install the native-runtime extra for protected native acceptance checks",
)
class NativeAcceptanceEvidenceTests(unittest.TestCase):
    def failed_observation(self, check):
        queue, github = Mock(), Mock()
        queue.reconcile.return_value = {
            "run": {
                "state": "completed",
                "native_outcome": "failed",
                "result_status": "failed",
                "result_url": "",
                "configuration_snapshot": {
                    "execution_kind": "native",
                    "autonomous": True,
                    "execution_policy": {"repair_limit": 0},
                },
                "source_snapshot": {"issue_number": 10, "repository_id": 99},
                "created_at": "2026-11-24T00:02:00+00:00",
                "finished_at": "2026-11-24T00:04:00+00:00",
            },
            "item": {
                "project": {"id": 1},
                "issue_number": 10,
                "source": "github",
                "github_repository_id": 99,
                "author_id": 7,
                "created_at": "2026-11-24T00:01:00+00:00",
                "branch_name": "work/failing-test",
            },
        }
        entries = [
            {
                "id": 1,
                "kind": "native_agent",
                "data": {
                    "successful": True,
                    "receipt": {"started_at": "2026-11-24T00:03:00+00:00"},
                },
            },
            {
                "id": 2,
                "kind": "native_intent",
                "operation_id": str(uuid4()),
                "data": {"kind": "agent", "attempt": 0},
            },
            {
                "id": 3,
                "kind": "native_validation",
                "data": {"checks": [{"id": check, "receipt": {"exit_code": 1}}]},
            },
        ]
        queue.request.return_value = {"evidence": entries, "has_more": False}
        github.repository.return_value = ({"repository": "owner/repo"}, "")
        github.request.side_effect = [
            {
                "number": 10,
                "user": {"id": 7},
                "created_at": "2026-11-24T00:00:00+00:00",
            },
            [],
        ]
        with patch("vibearr_runner.native_contract.validate_native_artifact"):
            return publication_acceptance.collect_run(
                queue, github, str(uuid4()), 1, 10, failed=True
            )

    def test_independent_runner_test_failure_establishes_failed_scenario(self):
        result = self.failed_observation("runner")
        self.assertEqual(result["nonzero_receipts"][0]["id"], "runner")
        self.assertTrue(result["no_pull_request"])

    def test_retired_cli_failure_cannot_establish_failed_scenario(self):
        with self.assertRaisesRegex(ValueError, "independent application or runner"):
            self.failed_observation("cli")

    def test_formatting_failure_cannot_replace_an_independent_test_failure(self):
        with self.assertRaisesRegex(ValueError, "independent application or runner"):
            self.failed_observation("ruff")

    def test_explicit_validation_profile_does_not_require_an_environment_fallback(self):
        from vibearr_executor import SLOT_QUALIFICATION_VERSION as QUALIFICATION_VERSION
        from vibearr_executor.compact_profiles import profiles
        from vibearr_runner.native_runtime import runtime_diagnostics
        from vibearr_runner.policy import default_execution_policy

        installed, validation = profiles("sha256:" + "d" * 64, "c" * 64)
        executor = Mock()
        executor.capabilities.return_value = {
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": QUALIFICATION_VERSION,
            "runtime": "compact-worker-v1",
            "qualified": True,
            "accepting_work": True,
            "binding_digest": "e" * 64,
            "profiles": [
                {**value.data(), "profile_digest": value.digest} for value in installed
            ],
        }
        policy = default_execution_policy()
        policy.update(validation_profile=validation.profile_id)
        with patch.dict("os.environ", {"VIBEARR_VALIDATION_PROFILE_FILE": ""}):
            result = runtime_diagnostics(
                policy, executor, validation_profile=validation
            )
        self.assertTrue(result["runtime_ready"], result["reason"])
        self.assertFalse(result["native_agent_ready"])

    def test_profiles_install_fixed_checks_without_agent_package_egress(self):
        from vibearr_executor.compact_profiles import profiles

        image = "sha256:" + "d" * 64
        installed, validation = profiles(image, "c" * 64)
        agent = next(
            value for value in installed if value.profile_id == "native-default"
        )
        self.assertNotIn("pypi.org", agent.network_hosts)
        self.assertNotIn("files.pythonhosted.org", agent.network_hosts)
        self.assertEqual(agent.backend_secret_env, "VIBEARR_NATIVE_CONNECTION")
        self.assertEqual(agent.database_image, image)
        checks = [value for value in installed if value.phase == "validation"]
        self.assertEqual(len(checks), len(validation.data()["required_checks"]))
        self.assertTrue(
            all(
                not value.backend_secret_env and not value.network_hosts
                for value in checks
            )
        )

    def test_non_native_run_cannot_claim_acceptance(self):
        queue = Mock()
        queue.reconcile.return_value = {
            "run": {
                "state": "completed",
                "native_outcome": "delivered",
                "result_status": "succeeded",
                "configuration_snapshot": {"execution_kind": "legacy"},
            },
            "item": {"project": {"id": 1}, "issue_number": 10},
        }
        with self.assertRaisesRegex(ValueError, "actual native lifecycle"):
            publication_acceptance.collect_run(queue, Mock(), str(uuid4()), 1, 10)

    def test_claimed_success_without_native_evidence_is_rejected(self):
        queue = Mock()
        queue.reconcile.return_value = {
            "run": {
                "state": "completed",
                "native_outcome": "delivered",
                "result_status": "succeeded",
                "configuration_snapshot": {
                    "execution_kind": "native",
                    "autonomous": True,
                },
                "source_snapshot": {"issue_number": 10, "repository_id": 99},
            },
            "item": {
                "project": {"id": 1},
                "issue_number": 10,
                "source": "github",
                "github_repository_id": 99,
            },
        }
        queue.request.side_effect = [
            {"evidence": [], "has_more": False},
        ]
        with self.assertRaisesRegex(ValueError, "genuine successful native"):
            publication_acceptance.collect_run(queue, Mock(), str(uuid4()), 1, 10)

    def test_manual_work_cannot_substitute_for_an_imported_owner_issue(self):
        queue = Mock()
        queue.reconcile.return_value = {
            "run": {
                "state": "completed",
                "native_outcome": "delivered",
                "result_status": "succeeded",
                "configuration_snapshot": {
                    "execution_kind": "native",
                    "autonomous": True,
                },
            },
            "item": {"project": {"id": 1}, "issue_number": 10, "source": "manual"},
        }
        with self.assertRaisesRegex(ValueError, "automatically imported issue"):
            publication_acceptance.collect_run(queue, Mock(), str(uuid4()), 1, 10)
