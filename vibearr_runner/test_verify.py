import contextlib
import io
import importlib.util
import unittest
from unittest.mock import patch

from vibearr_runner.verify import main


@unittest.skipUnless(
    importlib.util.find_spec("vibearr_executor"),
    "Install the native-runtime extra for deployment checks.",
)
class DeploymentVerificationTests(unittest.TestCase):
    def setUp(self):
        self.report = {
            "runtime": "compact-worker-v1",
            "protocol_version": 2,
            "qualification_version": 1,
            "qualified": True,
            "binding_digest": "a" * 64,
            "schema_version": 1,
        }
        self.client = patch("vibearr_executor.client.Client.from_environment").start()
        self.addCleanup(patch.stopall)
        self.client.return_value.capabilities.return_value = self.report

    def invoke(self, *arguments):
        with patch("sys.argv", ["verify", *arguments]):
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                return main()

    def test_current_qualified_dedicated_worker_is_accepted(self):
        self.assertEqual(self.invoke("--boundary"), 0)

    def test_stale_worker_rejects_deployment_readiness(self):
        self.report["qualified"] = False
        with self.assertRaisesRegex(
            RuntimeError, "current dedicated Docker worker boot"
        ):
            self.invoke("--boundary")

    def test_legacy_runtime_cannot_qualify_docker_deployment(self):
        self.report["runtime"] = "oci-executor"
        with self.assertRaises(RuntimeError):
            self.invoke("--boundary")

    def test_retired_host_flags_are_rejected_before_contacting_a_runtime(self):
        with self.assertRaises(SystemExit) as result:
            self.invoke("--boundary", "--executor-image", "sha256:" + "a" * 64)
        self.assertEqual(result.exception.code, 2)
        self.client.assert_not_called()

    def test_retired_host_flags_cannot_fall_back_to_portable_tests(self):
        with self.assertRaises(SystemExit) as result:
            self.invoke("--executor-image", "sha256:" + "a" * 64)
        self.assertEqual(result.exception.code, 2)
        self.client.assert_not_called()
