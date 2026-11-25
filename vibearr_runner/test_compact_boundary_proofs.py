import unittest
import importlib.util

from .compact_evidence import boundary_proof, digest


SDK_AVAILABLE = importlib.util.find_spec("vibearr_executor") is not None
if SDK_AVAILABLE:
    from vibearr_executor.compact_profiles import REQUIRED_BOUNDARY_CHECKS


@unittest.skipUnless(
    SDK_AVAILABLE, "Install the native-runtime extra for protected worker proof checks"
)
class BoundaryProofTests(unittest.TestCase):
    def setUp(self):
        self.authority = {
            "binding_digest": "a" * 64,
            "instance_id": "01234567-89ab-4cde-8f01-23456789abcd",
            "worker_boot": "12345678-9abc-4def-8012-3456789abcde",
            "image_digest": "sha256:" + "b" * 64,
        }
        self.boundary = {
            "kind": "actual-worker-boundary",
            "runtime": "compact-worker-v1",
            "passed": True,
            "fixtures": False,
            "checks": sorted(REQUIRED_BOUNDARY_CHECKS),
            "restart_receipt": "c" * 64,
            "receipts": [
                {
                    "operation_id": "23456789-abcd-4ef0-8123-456789abcdef",
                    "receipt_digest": "d" * 64,
                }
            ],
            **self.authority,
        }
        self.authority["qualification_digest"] = digest(self.boundary)

    def reseal(self):
        self.authority["qualification_digest"] = digest(self.boundary)

    def test_consistent_actual_boundary_shape_is_accepted(self):
        boundary_proof(self.boundary, self.authority)

    def test_failed_boundary_cannot_claim_qualified_authority(self):
        self.boundary["passed"] = False
        self.reseal()
        with self.assertRaises(ValueError):
            boundary_proof(self.boundary, self.authority)

    def test_fixture_boundary_cannot_claim_actual_authority(self):
        self.boundary["fixtures"] = True
        self.reseal()
        with self.assertRaises(ValueError):
            boundary_proof(self.boundary, self.authority)

    def test_resealed_binding_must_still_match_current_authority(self):
        self.boundary["binding_digest"] = "e" * 64
        self.reseal()
        with self.assertRaises(ValueError):
            boundary_proof(self.boundary, self.authority)

    def test_missing_probe_checks_cannot_claim_actual_authority(self):
        self.boundary["checks"] = []
        self.reseal()
        with self.assertRaises(ValueError):
            boundary_proof(self.boundary, self.authority)
