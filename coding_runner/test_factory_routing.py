from copy import deepcopy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from vibearr_runner.factory_routing import (
    FactoryRoutingError,
    REVIEWERS,
    default_configuration,
    normalize_configuration,
    require_factory_capabilities,
    require_route_capabilities,
    route,
)
from vibearr_runner.native_sessions import NativeSessionError


class FactoryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.configuration = default_configuration()
        self.intake = {"risk": "low", "surfaces": []}

    def test_existing_project_defaults_do_not_enable_factory(self):
        self.assertFalse(normalize_configuration({})["enabled"])

    def test_disabled_defaults_have_no_qualified_capability_dependency(self):
        self.assertEqual(require_factory_capabilities(self.configuration, {}), {})

    def test_missing_capabilities_cannot_enable_factory(self):
        self.configuration["enabled"] = True
        with self.assertRaises((NativeSessionError, ValueError)):
            require_factory_capabilities(self.configuration, {})

    def test_code_review_is_always_required(self):
        result = route(self.configuration, self.intake, ["work_queue/models.py"])
        self.assertEqual(result["required_reviewers"], ["reviewer-code"])

    def test_root_authentication_module_requires_security_review(self):
        result = route(self.configuration, self.intake, ["auth.py"])
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_root_credential_module_requires_security_review(self):
        result = route(self.configuration, self.intake, ["credentials.py"])
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_root_security_module_requires_security_review(self):
        result = route(self.configuration, self.intake, ["security.py"])
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_authority_boundaries_override_low_risk_intake(self):
        paths = [
            "runner_control/native_services.py",
            "runner_control/delivery.py",
            "vibearr_runner/native_preflight.py",
            "vibearr_runner/factory_routing.py",
            "vibearr_runner/policy.py",
            "vibearr_app/settings.py",
            "work_queue/services.py",
        ]
        for path in paths:
            with self.subTest(path=path):
                result = route(self.configuration, self.intake, [path])
                self.assertIn("reviewer-security", result["required_reviewers"])

    def test_dependency_manifests_require_security_review(self):
        for path in ("requirements.txt", "pyproject.toml", "web/package.json"):
            with self.subTest(path=path):
                result = route(self.configuration, self.intake, [path])
                self.assertIn("reviewer-security", result["required_reviewers"])

    def test_custom_rules_cannot_downgrade_authority_boundaries(self):
        self.configuration["path_rules"] = []
        self.configuration["known_paths"] = ["*"]
        result = route(
            self.configuration, self.intake, ["runner_control/native_services.py"]
        )
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_template_paths_add_both_ui_reviewers(self):
        result = route(
            self.configuration,
            self.intake,
            ["work_queue/templates/work_queue/index.html"],
        )
        self.assertEqual(
            result["required_reviewers"],
            ["reviewer-code", "reviewer-ui", "reviewer-ux"],
        )

    def test_security_paths_override_low_risk_intake(self):
        result = route(self.configuration, self.intake, ["credentials/models.py"])
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_intake_and_changed_path_surfaces_are_combined(self):
        self.intake["surfaces"] = ["security"]
        result = route(self.configuration, self.intake, ["templates/base.html"])
        self.assertEqual(set(result["required_reviewers"]), set(REVIEWERS))

    def test_unknown_path_requires_every_reviewer(self):
        result = route(self.configuration, self.intake, ["unrecognized/program.bin"])
        self.assertEqual(set(result["required_reviewers"]), set(REVIEWERS))
        self.assertEqual(result["risk"], "high")

    def test_unknown_surface_uses_registered_conservative_route(self):
        self.intake["surfaces"] = ["new-surface"]
        result = route(self.configuration, self.intake, ["README.md"])
        self.assertEqual(result["surfaces"], ["security", "ui"])
        self.assertEqual(set(result["required_reviewers"]), set(REVIEWERS))

    def test_renamed_old_path_cannot_hide_ui_change(self):
        paths = [
            {
                "path": "work_queue/services.py",
                "old_path": "templates/old.html",
                "status": "renamed",
            }
        ]
        result = route(self.configuration, self.intake, paths)
        self.assertIn("reviewer-ui", result["required_reviewers"])

    def test_deleted_security_path_is_still_reviewed(self):
        result = route(
            self.configuration,
            self.intake,
            [{"path": "credentials/auth.py", "status": "deleted"}],
        )
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_candidate_change_invalidates_path_digest(self):
        first = route(self.configuration, self.intake, ["work_queue/services.py"])
        second = route(self.configuration, self.intake, ["templates/base.html"])
        self.assertNotEqual(first["paths_digest"], second["paths_digest"])

    def test_prior_required_reviewers_survive_candidate_edits(self):
        first = route(self.configuration, self.intake, ["templates/base.html"])
        second = route(
            self.configuration, self.intake, ["work_queue/models.py"], previous=first
        )
        self.assertEqual(second["required_reviewers"], first["required_reviewers"])

    def test_previous_route_cannot_cross_configuration_versions(self):
        first = route(self.configuration, self.intake, ["templates/base.html"])
        self.configuration["max_repairs"] = 1
        with self.assertRaisesRegex(FactoryRoutingError, "different factory settings"):
            route(
                self.configuration,
                self.intake,
                ["work_queue/services.py"],
                previous=first,
            )

    def test_protected_validation_change_routes_security_at_high_risk(self):
        result = route(self.configuration, self.intake, [".github/workflows/test.yml"])
        self.assertEqual(result["protected_paths"], [".github/workflows/test.yml"])
        self.assertEqual(result["risk"], "high")
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_custom_rules_cannot_remove_builtin_security_route(self):
        self.configuration["path_rules"] = []
        result = route(self.configuration, self.intake, ["credentials/models.py"])
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_unknown_path_reviewers_cannot_be_downgraded(self):
        self.configuration["unknown_path_reviewers"] = ["reviewer-code"]
        result = route(self.configuration, self.intake, ["unknown.bin"])
        self.assertEqual(set(result["required_reviewers"]), set(REVIEWERS))

    def test_role_configuration_cannot_select_credentials_or_tools(self):
        self.configuration["roles"] = {"build": {"credential_id": 7}}
        with self.assertRaises(FactoryRoutingError):
            normalize_configuration(self.configuration)

    def test_path_traversal_cannot_be_routed_as_trusted_diff(self):
        with self.assertRaises(FactoryRoutingError):
            route(self.configuration, self.intake, ["../credentials/models.py"])

    def test_bool_cannot_be_a_repair_budget(self):
        self.configuration["max_repairs"] = True
        with self.assertRaises(FactoryRoutingError):
            normalize_configuration(self.configuration)

    def test_normalization_does_not_mutate_saved_settings(self):
        original = deepcopy(self.configuration)
        normalize_configuration(self.configuration)
        self.assertEqual(self.configuration, original)

    def test_custom_rules_cannot_overflow_normalized_rule_budget(self):
        self.configuration["path_rules"] = [
            {"pattern": f"custom/{index}", "surfaces": ["security"]}
            for index in range(100)
        ]
        with self.assertRaisesRegex(FactoryRoutingError, "including required routes"):
            normalize_configuration(self.configuration)

    def test_custom_protected_paths_cannot_overflow_normalized_budget(self):
        self.configuration["protected_paths"] = [
            f"custom/{index}" for index in range(100)
        ]
        with self.assertRaisesRegex(FactoryRoutingError, "including required paths"):
            normalize_configuration(self.configuration)

    def test_maximum_normalized_rules_remain_idempotent(self):
        available = 100 - len(self.configuration["path_rules"])
        self.configuration["path_rules"] += [
            {"pattern": f"custom/{index}", "surfaces": ["security"]}
            for index in range(available)
        ]
        normalized = normalize_configuration(self.configuration)
        self.assertEqual(normalize_configuration(normalized), normalized)

    def test_routing_is_independent_of_changed_path_order(self):
        first = route(
            self.configuration,
            self.intake,
            ["credentials/models.py", "templates/base.html"],
        )
        second = route(
            self.configuration,
            self.intake,
            ["templates/base.html", "credentials/models.py"],
        )
        self.assertEqual(first, second)

    def test_routed_capability_gate_cannot_skip_code_review(self):
        with self.assertRaisesRegex(FactoryRoutingError, "Code review"):
            require_route_capabilities({"required_reviewers": []}, {})


class FactoryGitRoutingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repository = Path(directory.name)
        self.git("init", "-q")

    def git(self, *arguments):
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Factory routing tests",
            "-c",
            "user.email=factory-routing@invalid",
            "-C",
            str(self.repository),
            *arguments,
        ]
        environment = dict(
            os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null"
        )
        return subprocess.check_output(command, env=environment)

    def rename_inventory(self, original, destination):
        source = self.repository / original
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("unchanged source retained across an exact rename\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Original source")
        base = self.git("rev-parse", "HEAD").decode().strip()
        target = self.repository / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        self.git("add", "-A")
        self.git("commit", "-qm", "Rename source")
        collapsed = self.git("diff", "--name-only", "-M", "-z", base, "HEAD", "--")
        self.assertEqual(collapsed.decode().split("\x00")[:-1], [destination])
        inventory = self.git(
            "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--"
        )
        paths = inventory.decode().split("\x00")[:-1]
        self.assertEqual(set(paths), {original, destination})
        return paths

    def test_real_git_inventory_preserves_security_rename(self):
        paths = self.rename_inventory("credentials/auth.py", "work_queue/models.py")
        result = route({}, {"risk": "low", "surfaces": []}, paths)
        self.assertIn("reviewer-security", result["required_reviewers"])

    def test_real_git_inventory_preserves_ui_rename(self):
        paths = self.rename_inventory("templates/base.html", "work_queue/models.py")
        result = route({}, {"risk": "low", "surfaces": []}, paths)
        self.assertIn("reviewer-ui", result["required_reviewers"])
        self.assertIn("reviewer-ux", result["required_reviewers"])

    def test_real_git_inventory_preserves_protected_rename(self):
        original = ".github/workflows/test.yml"
        paths = self.rename_inventory(original, "work_queue/models.py")
        result = route({}, {"risk": "low", "surfaces": []}, paths)
        self.assertEqual(result["protected_paths"], [original])
        self.assertEqual(result["risk"], "high")
        self.assertIn("reviewer-security", result["required_reviewers"])


class FactoryPreflightTests(unittest.TestCase):
    def setUp(self):
        from .test_native_preflight import NativePreflightTests

        self.fixture = NativePreflightTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()

    def test_enabled_factory_blocks_missing_roles_before_credentials_or_paid_probe(
        self,
    ):
        for project in self.fixture.configuration["projects"]:
            project["effective"]["factory"] = {"enabled": True}
        self.assertEqual(
            self.fixture.preflight.select(
                self.fixture.configuration, self.fixture.state
            ),
            [],
        )
        self.assertEqual(self.fixture.adapter.prepared, [])
        self.assertFalse(
            any(path.endswith("/secrets/") for _, path, _ in self.fixture.queue.calls)
        )
        self.assertIn(
            "factory",
            self.fixture.state["native_preflight"]["projects"]["1"]["reason"].lower(),
        )

    def test_disabled_factory_keeps_existing_native_readiness_path(self):
        for project in self.fixture.configuration["projects"]:
            project["effective"]["factory"] = {"enabled": False}
        self.fixture.preflight.select(self.fixture.configuration, self.fixture.state)
        self.assertEqual(len(self.fixture.adapter.starts), 1)
        self.fixture.adapter.state = "exited"
        self.assertEqual(
            self.fixture.preflight.select(
                self.fixture.configuration, self.fixture.state
            ),
            [1, 2],
        )


if __name__ == "__main__":
    unittest.main()
