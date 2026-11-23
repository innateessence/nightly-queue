import copy
import unittest

from .compact_evidence import (
    EVIDENCE,
    ROLE_NAMES,
    CompactEvidenceError,
    digest,
    validate_compact_report,
)


def valid_evidence():
    worker = {
        "binding_digest": "b" * 64,
        "instance_id": "01234567-89ab-4cde-8f01-23456789abcd",
        "worker_boot": "12345678-9abc-4def-8012-3456789abcde",
        "image_digest": "sha256:" + "a" * 64,
    }
    boundary = {
        "kind": "actual-worker-boundary",
        "runtime": "compact-worker-v1",
        "passed": True,
        "fixtures": False,
        **worker,
    }
    worker["qualification_digest"] = digest(boundary)
    roles = {}
    for role in ROLE_NAMES:
        access = "read_write" if role == "build" else "read_only"
        probe = "candidate_write_only" if role == "build" else "source_write_denied"
        proof = {
            "kind": "worker-boundary",
            "role": role,
            "source_access": access,
            "passed": True,
            "runtime_binding_digest": worker["binding_digest"],
            **{
                key: worker[key]
                for key in ("instance_id", "worker_boot", "image_digest")
            },
            "checks": dict.fromkeys(
                (
                    probe,
                    "control_paths_denied",
                    "credentials_scoped",
                    "descendants_terminated",
                ),
                True,
            ),
        }
        roles[role] = {"source_access": access, "qualification": proof}
    evidence = {}
    source = "c" * 40
    for name in EVIDENCE:
        document = {"kind": "unit-fixture-receipt", "gate": name}
        evidence[name] = {
            "kind": "actual-" + name,
            "passed": True,
            "fixtures": False,
            "source_revision": source,
            "receipts": [{"sha256": digest(document), "document": document}],
        }
    services = ["app", "postgres", "runner", "worker"]
    report = {
        "schema_version": 1,
        "kind": "compact-stack-engineering",
        "runtime": "compact-worker-v1",
        "passed": True,
        "fixtures": False,
        "services": services,
        "runtime_service_count": 4,
        "extra_runtime_services": [],
        "source_revision": source,
        "images": dict.fromkeys(services, worker["image_digest"]),
        "containers": {
            name: str(index + 1) * 64 for index, name in enumerate(services)
        },
        "worker": worker,
        "worker_boundary": boundary,
        "evidence": evidence,
    }
    capabilities = {
        "runtime": "compact-worker-v1",
        "qualified": True,
        "factory_roles": roles,
        **worker,
    }
    return report, capabilities


class CompactEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.report, self.capabilities = valid_evidence()

    def test_complete_report_matches_current_authority(self):
        self.assertEqual(
            validate_compact_report(self.report, self.capabilities), self.report
        )

    def test_missing_observation_is_rejected(self):
        del self.report["evidence"]["browser"]
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)

    def test_changed_receipt_document_is_rejected(self):
        receipt = self.report["evidence"]["browser"]["receipts"][0]
        receipt["document"]["gate"] = "another-gate"
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)

    def test_digest_without_document_is_rejected(self):
        del self.report["evidence"]["browser"]["receipts"][0]["document"]
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)

    def test_previous_boot_report_is_rejected(self):
        self.capabilities["worker_boot"] = "23456789-abcd-4ef0-8123-456789abcdef"
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)

    def test_read_only_role_without_write_denial_is_rejected(self):
        checks = self.capabilities["factory_roles"]["reviewer-code"]["qualification"][
            "checks"
        ]
        checks["source_write_denied"] = False
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)

    def test_reusing_container_identity_is_rejected(self):
        self.report["containers"]["runner"] = self.report["containers"]["worker"]
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)

    def test_source_mismatch_in_gate_is_rejected(self):
        self.report["evidence"]["browser"]["source_revision"] = "d" * 40
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)

    def test_boundary_document_change_is_rejected(self):
        self.report["worker_boundary"] = copy.deepcopy(self.report["worker_boundary"])
        self.report["worker_boundary"]["passed"] = False
        with self.assertRaises(CompactEvidenceError):
            validate_compact_report(self.report, self.capabilities)
