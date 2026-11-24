from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
import unittest


class CommandFailureTests(unittest.TestCase):
    def section(self, filename, start, end):
        source = Path(__file__).with_name(filename).read_text()
        return source[source.index(start) : source.index(end, source.index(start))]

    def run_shell(self, script):
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_failed_target_listing_stops_before_inspection(self):
        function = self.section("checks.sh", "qualify_worker() {", "observe_phase() {")
        stub = "compose() { printf '%064d\\n' 1 2 3; return 17; }; docker() { echo INSPECTED; return 97; };\n"
        result = self.run_shell(stub + function + "\nqualify_worker\n")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertNotIn("INSPECTED", result.stdout)
        self.assertIn("stage=select status=17", result.stderr)

    def test_failed_target_inspection_records_exact_status(self):
        function = self.section("checks.sh", "qualify_worker() {", "observe_phase() {")
        stub = """
VIBEARR_TOOLS_IMAGE=unused
compose() { printf '%064d\\n' 1 2 3; }
docker() { if [[ $1 == inspect ]]; then return 23; fi; cat >/dev/null; echo '{}'; }
"""
        result = self.run_shell(stub + function + "\nqualify_worker\n")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIn("stage=inspect-targets status=23", result.stderr)
        self.assertNotIn("stage=qualify-cli", result.stderr)

    def qualification_cli(self, status):
        function = self.section("checks.sh", "qualify_worker() {", "observe_phase() {")
        stub = """
VIBEARR_TOOLS_IMAGE=unused
compose() {
  if [[ $1 == ps ]]; then printf '%064d\\n' 1 2 3; return; fi
  if ((CLI_STATUS == 0)); then echo '{"passed":true}'; fi
  return "$CLI_STATUS"
}
docker() { if [[ $1 == inspect ]]; then echo '[]'; return; fi; cat >/dev/null; echo '{}'; }
"""
        return self.run_shell(
            "CLI_STATUS=" + str(status) + "\n" + stub + function + "\nqualify_worker\n"
        )

    def test_killed_qualification_cli_records_and_preserves_137(self):
        result = self.qualification_cli(137)
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("stage=qualify-cli status=137", result.stderr)

    def test_qualification_diagnostics_leave_success_json_on_stdout(self):
        result = self.qualification_cli(0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '{"passed":true}\n')
        self.assertIn("stage=qualify-cli status=0", result.stderr)

    def failed_cleanup(self, original_status, extra_stub=""):
        function = self.section("checks.sh", "cleanup() {", "trap cleanup EXIT")
        stub = """
project=probe
compose() { return 1; }; restored() { return 1; }
export_volume() { return 1; }; docker() { return 1; }
failure_state() { echo '{"oom_killed":false}'; }
"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "output="
                + shlex.quote(str(root))
                + "\nscratch="
                + shlex.quote(str(root / "scratch"))
                + "\n"
                + stub
                + extra_stub
                + function
                + "\ntrap cleanup EXIT\nexit "
                + str(original_status)
            )
            result = self.run_shell(script)
            evidence = (root / "exit-status.json").read_text()
        return result, evidence

    def test_cleanup_failure_preserves_original_137(self):
        result, evidence = self.failed_cleanup(137)
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertEqual(
            evidence, '{"original_status":137,"cleanup_status":1,"final_status":137}\n'
        )

    def test_cleanup_failure_fails_an_otherwise_successful_run(self):
        result, evidence = self.failed_cleanup(0)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(
            evidence, '{"original_status":0,"cleanup_status":1,"final_status":1}\n'
        )

    def test_scratch_removal_failure_preserves_original_137(self):
        result, evidence = self.failed_cleanup(137, "rm() { return 31; };\n")
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertIn('"original_status":137', evidence)

    def test_status_write_failure_preserves_original_137(self):
        result, evidence = self.failed_cleanup(137, "printf() { return 32; };\n")
        self.assertEqual(result.returncode, 137, result.stderr)
        self.assertEqual(evidence, "")

    def test_status_write_failure_fails_an_otherwise_successful_run(self):
        stub = "printf() { return 32; }; export_volume() { :; }; compose() { :; }; restored() { :; };\n"
        result, evidence = self.failed_cleanup(0, stub)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(evidence, "")

    def test_failed_phase_listing_does_not_write_count_proof(self):
        function = self.section("checks.sh", "observe_phase() {", "export_volume() {")
        stub = "project=probe; docker() { printf '%064d\\n' 1 2 3 4; return 24; };\n"
        with TemporaryDirectory() as directory:
            result = self.run_shell(
                "output="
                + directory
                + "\n"
                + stub
                + function
                + "\nobserve_phase startup\n"
            )
            self.assertFalse((Path(directory) / "phase-counts.jsonl").exists())
        self.assertEqual(result.returncode, 24, result.stderr)

    def test_failed_initial_listing_stops_before_audit(self):
        section = self.section("checks.sh", "id_listing=", "((${#ids[@]}")
        stub = "compose() { printf '%064d\\n' 1 2 3 4; return 25; };\n"
        result = self.run_shell(stub + section + "\necho AUDITED\n")
        self.assertEqual(result.returncode, 25, result.stderr)
        self.assertNotIn("AUDITED", result.stdout)

    def test_failed_binding_parser_stops_before_candidate_checks(self):
        section = self.section(
            "checks.sh", "  binding_listing=", "  candidate_result=0"
        )
        stub = "VIBEARR_TOOLS_IMAGE=unused; docker() { printf '%064d\\n' 1 2 3; return 26; };\n"
        with TemporaryDirectory() as directory:
            (Path(directory) / "source.json").write_text("{}")
            result = self.run_shell(
                "output=" + directory + "\n" + stub + section + "\necho VALIDATED\n"
            )
        self.assertEqual(result.returncode, 26, result.stderr)
        self.assertNotIn("VALIDATED", result.stdout)

    def test_failed_image_listing_stops_before_startup(self):
        source = Path(__file__).parents[2] / "run.sh"
        text = source.read_text()
        section = text[text.index("image_listing=") : text.index("while IFS=")]
        stub = "docker() { printf '%s\\n' runner-image worker-image; return 27; };\n"
        result = self.run_shell(stub + section + "\necho STARTED\n")
        self.assertEqual(result.returncode, 27, result.stderr)
        self.assertNotIn("STARTED", result.stdout)

    def test_failed_image_inspection_cannot_register_partial_role_output(self):
        source = Path(__file__).parents[2] / "run.sh"
        text = source.read_text()
        section = text[
            text.index("while IFS=") : text.index(
                "export VIBEARR_RUNNER_IMAGE_ID VIBEARR_WORKER_IMAGE_ID"
            )
        ]
        stub = """
image_listing=$'runner-image\\nworker-image'
VIBEARR_RUNNER_IMAGE_ID=''
VIBEARR_WORKER_IMAGE_ID=''
docker() { printf '%s sha256:%064d\\n' "${3%-image}" 1; return 28; }
"""
        result = self.run_shell(stub + section + "\necho STARTED\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("STARTED", result.stdout)

    def test_failed_existing_container_query_stops_preflight(self):
        section = self.section("checks.sh", "for selected in", "scratch=")
        stub = (
            "project=probe; restore_project=probe-restore; docker() { return 29; };\n"
        )
        result = self.run_shell(stub + section + "\necho STARTED\n")
        self.assertEqual(result.returncode, 29, result.stderr)
        self.assertNotIn("STARTED", result.stdout)

    def test_failed_existing_volume_query_stops_preflight(self):
        section = self.section("checks.sh", "for selected in", "scratch=")
        stub = "project=probe; restore_project=probe-restore; docker() { if [[ $1 == ps ]]; then return; fi; return 30; };\n"
        result = self.run_shell(stub + section + "\necho STARTED\n")
        self.assertEqual(result.returncode, 30, result.stderr)
        self.assertNotIn("STARTED", result.stdout)

    def check_failed_base_inspection(self, source):
        text = source.read_text()
        section = text[
            text.index("worker_base_after=") : text.index("VIBEARR_RUNNER_IMAGE_ID=")
        ]
        stub = "VIBEARR_WORKER_BASE_IMAGE=base; VIBEARR_WORKER_BASE_IMAGE_ID=selected; docker() { echo selected; return 31; };\n"
        result = self.run_shell(stub + section + "\necho STARTED\n")
        self.assertEqual(result.returncode, 31, result.stderr)
        self.assertNotIn("STARTED", result.stdout)

    def test_failed_worker_base_inspection_stops_qualification(self):
        self.check_failed_base_inspection(Path(__file__).with_name("checks.sh"))

    def test_failed_worker_base_inspection_stops_startup(self):
        self.check_failed_base_inspection(Path(__file__).parents[2] / "run.sh")

    def test_failed_deployment_workflow_listing_stops_audit(self):
        source = (
            Path(__file__).parents[2]
            / ".github/workflows/runner-deployment-boundary.yml"
        )
        text = source.read_text()
        section = text[
            text.index("          id_listing=") : text.index("          docker exec")
        ]
        stub = "PROJECT=probe; docker() { printf '%064d\\n' 1 2 3 4; return 32; };\n"
        result = self.run_shell(stub + section + "\necho AUDITED\n")
        self.assertEqual(result.returncode, 32, result.stderr)
        self.assertNotIn("AUDITED", result.stdout)

    def test_failed_deployment_worker_listing_stops_audit(self):
        source = (
            Path(__file__).parents[2]
            / ".github/workflows/runner-deployment-boundary.yml"
        )
        text = source.read_text()
        section = text[
            text.index("          id_listing=") : text.index("          docker exec")
        ]
        stub = "PROJECT=probe; docker() { if [[ $* == *service=worker* ]]; then printf '%064d\\n' 1; return 33; fi; printf '%064d\\n' 1 2 3 4; };\n"
        result = self.run_shell(stub + section + "\necho AUDITED\n")
        self.assertEqual(result.returncode, 33, result.stderr)
        self.assertNotIn("AUDITED", result.stdout)

    def test_portable_verify_uses_a_writable_copy_of_the_source(self):
        text = Path(__file__).with_name("checks.sh").read_text()
        line = next(
            line for line in text.splitlines() if '> "$output/tools-tests.log"' in line
        )
        command = line.split("-c '", 1)[1].split("' >", 1)[0]
        stub = """
black() { :; }; ruff() { :; }; mypy() { :; }; shellcheck() { :; }; actionlint() { :; }
python() { if [[ $* == '-m vibearr_runner.verify' ]]; then mkdir permission-probe; fi; }
"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "immutable-source"
            copied = root / "portable-source"
            source.mkdir()
            (source / "source-marker").write_text("same source")
            command = command.replace("/tmp/portable-source", shlex.quote(str(copied)))
            command = command.replace("/app", shlex.quote(str(source)))
            script = "cd " + shlex.quote(str(source)) + "\n" + stub + command
            result = self.run_shell(script)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((copied / "source-marker").read_text(), "same source")
            self.assertTrue((copied / "permission-probe").is_dir())
            self.assertFalse((source / "permission-probe").exists())

    def test_backup_manifest_reader_has_only_read_override_on_one_private_mount(self):
        text = Path(__file__).with_name("checks.sh").read_text()
        line = next(
            line
            for line in text.splitlines()
            if '> "$output/backup-manifest.json"' in line
        )
        arguments = shlex.split(line)
        added = [
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "--cap-add"
        ]
        mounts = [
            arguments[index + 1]
            for index, value in enumerate(arguments)
            if value == "--volume"
        ]
        self.assertEqual(added, ["DAC_OVERRIDE"])
        self.assertEqual(mounts, ["$VIBEARR_BACKUP_VOLUME:/backups:ro"])
        self.assertEqual(arguments[arguments.index("--cap-drop") + 1], "ALL")
        self.assertEqual(arguments[arguments.index("--network") + 1], "none")
        self.assertIn("--read-only", arguments)
        self.assertEqual(arguments[arguments.index("--user") + 1], "0:0")
        self.assertEqual(arguments[arguments.index("--memory") + 1], "128m")
        self.assertEqual(arguments[arguments.index("--pids-limit") + 1], "16")
        self.assertEqual(arguments[arguments.index("--cpus") + 1], "1")


class NativeCodexImageFailureTests(unittest.TestCase):
    def run_check(self, directory, commit, docker_status=31):
        script = Path(__file__).with_name("check-native-codex.sh")
        stub = (
            "docker() { echo DOCKER_CALLED; return "
            + str(docker_status)
            + "; }; export -f docker; "
        )
        command = (
            stub
            + "VIBEARR_CHECK_OUTPUT="
            + shlex.quote(str(Path(directory) / "evidence"))
            + " bash "
            + shlex.quote(str(script))
            + " --candidate "
            + shlex.quote(directory)
            + " --commit "
            + shlex.quote(commit)
        )
        return subprocess.run(
            ["bash", "-c", command], capture_output=True, text=True, timeout=10
        )

    def test_invalid_candidate_sha_stops_before_docker(self):
        with TemporaryDirectory() as directory:
            result = self.run_check(directory, "main")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("DOCKER_CALLED", result.stdout)

    def test_failed_bootstrap_build_preserves_exact_exit_status(self):
        with TemporaryDirectory() as directory:
            result = self.run_check(directory, "a" * 40)
        self.assertEqual(result.returncode, 31, result.stderr)

    def test_version_mismatch_fails_before_contract_tests(self):
        script = Path(__file__).with_name("check-native-codex.sh").read_text()
        block = script[script.index("expected=$(docker run") :]
        stub = """set -euo pipefail
output=$1
tools=tools
runner=runner
docker() {
    if [[ $* == *'--entrypoint python '* ]]; then echo 'codex-cli 0.169.0'; return; fi
    if [[ $* == *'--entrypoint codex '* ]]; then echo 'codex-cli 0.168.0'; return; fi
    echo CONTRACT_TESTS_CALLED
}
"""
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", "-c", stub + block, "check", directory],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("CONTRACT_TESTS_CALLED", result.stdout)
