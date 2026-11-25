import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from vibearr_runner.bootstrap_files import private_json
from vibearr_runner import self_acceptance


class AcceptanceTests(unittest.TestCase):
    def test_protected_epoch_receipt_time_matches_server_iso_time(self):
        epoch = 1794852047.8716285
        iso = datetime.fromtimestamp(epoch, timezone.utc).isoformat()
        self.assertEqual(
            self_acceptance.timestamp(epoch), self_acceptance.timestamp(iso)
        )
        for invalid in (True, None, float("nan"), float("inf"), -1):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                self_acceptance.timestamp(invalid)

    def test_missing_execute_flag_never_constructs_api_client(self):
        with (
            patch("sys.argv", ["self_acceptance"]),
            patch.object(self_acceptance, "client") as client,
        ):
            with self.assertRaises(SystemExit):
                self_acceptance.main()
        client.assert_not_called()

    def test_scenarios_keep_merge_ui_work_separate_from_cli_work(self):
        scenarios = self_acceptance.scenario_tasks()
        self.assertEqual(scenarios["delivery"]["order"][0], "version-regression")
        self.assertEqual(scenarios["delivery"]["failure_repair_limit"], 0)
        self.assertEqual(scenarios["delivery"]["normal_repair_limit"], 2)
        self.assertIn("status-filter", scenarios["merge"]["tasks"][0]["body"])
        self.assertNotIn("Add a queue status filter", repr(scenarios["delivery"]))
        self.assertTrue(scenarios["merge"]["available"])

    def test_merge_scenario_delegates_to_exact_two_issue_driver(self):
        with (
            patch(
                "sys.argv",
                [
                    "self_acceptance",
                    "--execute",
                    "--scenario=merge",
                    "--project-id",
                    "1",
                    "--first-issue",
                    "12",
                    "--second-issue",
                    "13",
                    "--state",
                    "/tmp/owned-state",
                    "--evidence",
                    "/tmp/owned-evidence",
                ],
            ),
            patch("vibearr_runner.merge_acceptance.main", return_value=0) as merge_main,
        ):
            self.assertEqual(self_acceptance.main(), 0)
        arguments = merge_main.call_args.args[0]
        self.assertIn("--execute", arguments)
        self.assertEqual(arguments[arguments.index("--second-issue") + 1], "13")

    def test_evidence_history_fetches_all_pages(self):
        queue = Mock()
        queue.request.side_effect = [
            {"evidence": [{"id": 1}], "has_more": True, "next_after": 1},
            {"evidence": [{"id": 2}], "has_more": False, "next_after": None},
        ]
        result = self_acceptance.evidence_entries(queue, str(uuid4()))
        self.assertEqual(result, [{"id": 1}, {"id": 2}])
        self.assertEqual(queue.request.call_args.kwargs["query"], {"after": 1})

    def test_source_improvement_cannot_claim_under_failure_only_repair_policy(self):
        queue = Mock()
        queue.active.return_value = {"run": None, "item": None}
        queue.configuration.return_value = {
            "projects": [
                {
                    "project_id": 1,
                    "execution_policy": {
                        "repair_limit": 0,
                        "delivery_mode": "pull_request",
                    },
                }
            ]
        }
        self_acceptance.verify_phase_policy(queue, 1, 3, failed=True)
        with self.assertRaisesRegex(ValueError, "restore normal repair_limit"):
            self_acceptance.verify_phase_policy(queue, 1, 4, failed=False)
        queue.configuration.return_value["projects"][0]["execution_policy"][
            "repair_limit"
        ] = 2
        self_acceptance.verify_phase_policy(queue, 1, 4, failed=False)

    def test_matching_active_attempt_reconciles_without_demanding_a_new_claim_policy(
        self,
    ):
        queue = Mock()
        queue.active.return_value = {
            "run": {"id": "original"},
            "item": {"project": {"id": 1}, "issue_number": 3},
        }
        self_acceptance.verify_phase_policy(
            queue, 1, 3, failed=True, existing_run="original"
        )
        queue.configuration.assert_not_called()

    def test_evidence_nonadvancing_cursor_is_rejected(self):
        queue = Mock()
        queue.request.return_value = {
            "evidence": [{"id": 1}],
            "has_more": True,
            "next_after": 0,
        }
        with self.assertRaisesRegex(ValueError, "cursor"):
            self_acceptance.evidence_entries(queue, str(uuid4()))

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
            self_acceptance.restart_evidence(record, receipt)

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
            self_acceptance.restart_evidence(record, receipt)

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
            self_acceptance.observe_pull(self.github, self.delivery)

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
        _, original, current = self_acceptance.observe_pull(
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
            self_acceptance.observe_pull(self.github, self.delivery, self.snapshot())

    def test_saved_observation_cannot_replace_immutable_server_delivery(self):
        snapshot = self.snapshot()
        snapshot["delivery"] = {"different": "receipt"}
        with self.assertRaisesRegex(ValueError, "Immutable server delivery"):
            self_acceptance.observe_pull(self.github, self.delivery, snapshot)

    def test_incomplete_saved_provider_observation_is_not_proof(self):
        snapshot = self.snapshot()
        snapshot["provider_delivery_snapshot"].pop("observed_at")
        with self.assertRaisesRegex(ValueError, "observation time"):
            self_acceptance.observe_pull(self.github, self.delivery, snapshot)

    def test_branch_query_does_not_interpret_task_text_as_query_parameters(self):
        from urllib.parse import parse_qs, urlsplit

        self_acceptance.pulls_for_branch(
            self.github, "owner/repo", "acceptance/task&state=closed"
        )
        query = parse_qs(urlsplit(self.github.request.call_args.args[1]).query)
        self.assertEqual(query["state"], ["all"])
        self.assertEqual(query["head"], ["owner:acceptance/task&state=closed"])

    def test_documentation_only_delivery_cannot_substitute_for_the_approved_cli_improvements(
        self,
    ):
        records = [
            {"run_id": "failed", "issue_number": 1},
            {"run_id": "first", "issue_number": 2, "changed_files": ["docs/note.md"]},
            {
                "run_id": "second",
                "issue_number": 3,
                "changed_files": ["vibearr/cli.py"],
            },
        ]
        with patch.object(self_acceptance, "collect_run", side_effect=records):
            with self.assertRaisesRegex(ValueError, "real production CLI source"):
                self_acceptance.collect_scenario(
                    Mock(),
                    Mock(),
                    project_id=1,
                    failure=("failed", 1),
                    first=("first", 2),
                    second=("second", 3),
                )


@unittest.skipUnless(
    importlib.util.find_spec("vibearr_executor"),
    "Install the native-runtime extra for real native receipt parsing",
)
class NativeAcceptanceEvidenceTests(unittest.TestCase):
    def test_explicit_validation_profile_does_not_require_an_environment_fallback(self):
        from vibearr_executor import SLOT_QUALIFICATION_VERSION as QUALIFICATION_VERSION
        from vibearr_executor.validation_profile import ValidationProfile
        from vibearr_runner.native_codex import codex_profile
        from vibearr_runner.native_runtime import runtime_diagnostics
        from vibearr_runner.policy import default_execution_policy

        validation = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64
        )
        profiles = [
            codex_profile("sha256:" + "d" * 64, "token"),
            *validation.executor_profiles(),
        ]
        executor = Mock()
        executor.capabilities.return_value = {
            "schema_version": 1,
            "protocol_version": 2,
            "qualification_version": QUALIFICATION_VERSION,
            "runtime": "compose-slot-v2",
            "qualified": True,
            "accepting_work": True,
            "binding_digest": "e" * 64,
            "profiles": [
                {**value.data(), "profile_digest": value.digest} for value in profiles
            ],
        }
        policy = default_execution_policy()
        policy.update(
            runtime_profile="codex-token-v1", validation_profile=validation.profile_id
        )
        with patch.dict("os.environ", {"VIBEARR_VALIDATION_PROFILE_FILE": ""}):
            result = runtime_diagnostics(
                policy, executor, validation_profile=validation
            )
        self.assertTrue(result["runtime_ready"], result["reason"])

    def test_profile_selection_adds_all_checks_without_agent_package_egress(self):
        from vibearr_executor.validation_profile import ValidationProfile
        from vibearr_runner.bootstrap_profiles import profiles

        validation = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64
        )
        values = profiles("sha256:" + "d" * 64, validation, "token")
        self.assertEqual(len(values), 11)
        agent = next(value for value in values if value["phase"] == "agent")
        self.assertEqual(agent["network_hosts"], ["api.openai.com"])
        self.assertEqual(agent["database_image"], validation.database_image)
        checks = [value for value in values if value["phase"] == "validation"]
        self.assertEqual(len(checks), 9)
        self.assertTrue(
            all(
                not value["backend_secret_env"] and not value["network_hosts"]
                for value in checks
            )
        )

    def test_existing_profile_choice_cannot_be_replaced(self):
        from vibearr_executor.validation_profile import ValidationProfile
        from vibearr_runner.bootstrap_profiles import profiles

        validation = ValidationProfile(
            "sha256:" + "a" * 64, "sha256:" + "b" * 64, "c" * 64
        )
        original = profiles("sha256:" + "d" * 64, validation, "token")
        with self.assertRaisesRegex(ValueError, "differs"):
            profiles("sha256:" + "e" * 64, validation, "token", original)

    def test_private_profile_writer_is_idempotent_and_preserves_other_choices(self):
        from vibearr_runner.bootstrap_profiles import save_new

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profiles.json"
            self.assertTrue(save_new(path, [{"profile_id": "selected"}]))
            original = path.stat().st_mtime_ns
            self.assertFalse(save_new(path, [{"profile_id": "selected"}]))
            self.assertEqual(path.stat().st_mtime_ns, original)
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            with self.assertRaisesRegex(ValueError, "never overwrites"):
                save_new(path, [{"profile_id": "different"}])

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
            self_acceptance.collect_run(queue, Mock(), str(uuid4()), 1, 10)

    def test_approval_record_prevents_native_acceptance(self):
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
        queue.request.return_value = {
            "approvals": [{"consumed_at": "2026-11-16T00:00:00+00:00"}]
        }
        with self.assertRaisesRegex(ValueError, "legacy approvals"):
            self_acceptance.collect_run(queue, Mock(), str(uuid4()), 1, 10)

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
            {"approvals": []},
            {"evidence": [], "has_more": False},
        ]
        with self.assertRaisesRegex(ValueError, "genuine successful native"):
            self_acceptance.collect_run(queue, Mock(), str(uuid4()), 1, 10)

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
            self_acceptance.collect_run(queue, Mock(), str(uuid4()), 1, 10)
