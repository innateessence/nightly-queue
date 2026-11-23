from pathlib import Path
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
        stub = "compose() { printf '%064d\\n' 1 2 3; return 23; }; docker() { echo INSPECTED; return 97; };\n"
        result = self.run_shell(stub + function + "\nqualify_worker\n")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotIn("INSPECTED", result.stdout)

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
