"""Wheel contract unit fixtures do not establish runtime qualification."""

import base64
import copy
import csv
import hashlib
import io
import json
import unittest
import zipfile

from .compact_evidence import validate_wheels


def wheel(name, entrypoints, *, corrupt_record=False, comment=b""):
    directory = name.replace("-", "_") + "-0.1.0.dist-info"
    points = "[console_scripts]\n" + "".join(
        f"{key} = {value}\n" for key, value in entrypoints.items()
    )
    files = {
        directory
        + "/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.1.0\n".encode(),
        directory
        + "/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        directory + "/entry_points.txt": points.encode(),
    }
    for target in entrypoints.values():
        files[target.split(":")[0].replace(".", "/") + ".py"] = (
            b"def main():\n    return 0\n"
        )
    records = io.StringIO()
    writer = csv.writer(records)
    for filename, content in files.items():
        checksum = (
            base64.urlsafe_b64encode(hashlib.sha256(content).digest())
            .rstrip(b"=")
            .decode()
        )
        writer.writerow((filename, "sha256=" + checksum, len(content)))
    writer.writerow((directory + "/RECORD", "", ""))
    files[directory + "/RECORD"] = records.getvalue().encode()
    if corrupt_record:
        files[directory + "/METADATA"] += b"Summary: changed after sealing\n"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for filename, content in files.items():
            archive.writestr(filename, content)
        archive.comment = comment
    content = stream.getvalue()
    return {
        "filename": name.replace("-", "_") + "-0.1.0-py3-none-any.whl",
        "sha256": hashlib.sha256(content).hexdigest(),
        "content": base64.b64encode(content).decode(),
    }


def seal(payload):
    proof = {
        "kind": "offline-packaging-artifacts",
        "wheels": payload["wheels"],
        "commands": payload["commands"],
    }
    artifact = json.dumps(proof, sort_keys=True).encode()
    payload["artifact_proof"] = base64.b64encode(artifact).decode()
    payload["operation"] = {
        "result": {
            "artifact": {
                "files": [
                    {
                        "path": "packaging-proof.json",
                        "sha256": hashlib.sha256(artifact).hexdigest(),
                        "size": len(artifact),
                    }
                ]
            }
        }
    }
    return payload


def evidence(executor=None, runner=None):
    wheels = [
        wheel(
            "vibearr-runner",
            runner or {"vibearr-runner": "vibearr_runner.__main__:main"},
        ),
        wheel(
            "vibearr-executor",
            executor or {"vibearr-worker": "vibearr_executor.compact_worker:main"},
        ),
    ]
    commands = [
        {
            "argv": [
                "/venv/bin/python",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "/wheels/vibearr_runner.whl",
            ],
            "exit_code": 0,
            "stdout_sha256": "a" * 64,
        }
    ]
    for name in ("vibearr-runner", "vibearr-worker"):
        commands.append(
            {
                "argv": ["/venv/bin/" + name, "--help"],
                "exit_code": 0,
                "stdout_sha256": "b" * 64,
            }
        )
    return seal({"wheels": wheels, "commands": commands})


class PackagingEvidenceTests(unittest.TestCase):
    def test_current_runtime_console_commands_are_accepted(self):
        validate_wheels(evidence())

    def test_separate_executor_builds_remain_valid_distribution_evidence(self):
        payload = evidence()
        payload["wheels"].append(
            wheel(
                "vibearr-executor",
                {"vibearr-worker": "vibearr_executor.compact_worker:main"},
                comment=b"separate build",
            )
        )
        validate_wheels(seal(payload))

    def test_retired_executor_command_is_rejected(self):
        payload = evidence(
            executor={"vibearr-executor": "vibearr_executor.server:main"}
        )
        with self.assertRaisesRegex(ValueError, "entrypoints"):
            validate_wheels(payload)

    def test_legacy_cli_alias_is_rejected(self):
        payload = evidence(
            runner={
                "vibearr-runner": "vibearr_runner.__main__:main",
                "nq": "vibearr_runner.__main__:main",
            }
        )
        with self.assertRaisesRegex(ValueError, "entrypoints"):
            validate_wheels(payload)

    def test_removed_vibearr_distribution_is_rejected(self):
        payload = evidence()
        payload["wheels"].append(wheel("vibearr-cli", {"vibearr": "vibearr.cli:main"}))
        with self.assertRaisesRegex(ValueError, "Unexpected distribution"):
            validate_wheels(seal(payload))

    def test_removed_vibearr_entrypoint_is_rejected(self):
        payload = evidence(
            runner={
                "vibearr-runner": "vibearr_runner.__main__:main",
                "vibearr": "vibearr.cli:main",
            }
        )
        with self.assertRaisesRegex(ValueError, "entrypoints"):
            validate_wheels(payload)

    def test_removed_vibearr_help_evidence_is_rejected(self):
        payload = evidence()
        payload["commands"].append(
            {
                "argv": ["/venv/bin/vibearr", "--help"],
                "exit_code": 0,
                "stdout_sha256": "c" * 64,
            }
        )
        with self.assertRaisesRegex(ValueError, "execution evidence"):
            validate_wheels(seal(payload))

    def test_missing_runner_distribution_is_rejected(self):
        payload = evidence()
        payload["wheels"].pop(0)
        with self.assertRaisesRegex(ValueError, "required distribution"):
            validate_wheels(seal(payload))

    def test_current_command_cannot_target_the_retired_service(self):
        payload = evidence(executor={"vibearr-worker": "vibearr_executor.server:main"})
        with self.assertRaisesRegex(ValueError, "entrypoints"):
            validate_wheels(payload)

    def test_missing_installed_worker_help_is_rejected(self):
        payload = evidence()
        payload["commands"].pop()
        with self.assertRaisesRegex(ValueError, "execution evidence"):
            validate_wheels(seal(payload))

    def test_failed_installed_worker_help_is_rejected(self):
        payload = evidence()
        payload["commands"][-1]["exit_code"] = 1
        with self.assertRaisesRegex(ValueError, "execution check failed"):
            validate_wheels(seal(payload))

    def test_tampered_wheel_record_is_rejected(self):
        payload = evidence()
        payload["wheels"][-1] = wheel(
            "vibearr-executor",
            {"vibearr-worker": "vibearr_executor.compact_worker:main"},
            corrupt_record=True,
        )
        with self.assertRaisesRegex(ValueError, "RECORD"):
            validate_wheels(seal(payload))

    def test_substituted_command_evidence_is_rejected(self):
        payload = copy.deepcopy(evidence())
        payload["commands"][-1]["stdout_sha256"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "artifact differs"):
            validate_wheels(payload)
