import unittest

from .compact_evidence import CompactEvidenceError, validate_observations


class RuntimeObservationTests(unittest.TestCase):
    def setUp(self):
        self.containers = {
            name: str(index + 1) * 64
            for index, name in enumerate(("app", "postgres", "runner", "worker"))
        }
        identities = list(self.containers.values())
        idle = [
            {
                "ID": identity,
                "CPUPerc": "0.00%",
                "MemPerc": "1.00%",
                "PIDs": "10",
                "MemUsage": "10MiB / 1GiB",
            }
            for identity in identities
        ]
        phases = [
            {"phase": phase, "containers": list(identities)}
            for phase in (
                "startup",
                "boundary",
                "dependency",
                "candidate",
                "browser",
                "beforebackup",
            )
        ]
        peaks = {"memory_peak": "4294967296", "pids_peak": "128"}
        self.value = {
            "runtime_observations": {
                "idle": idle,
                "phases": phases,
                "worker_peak": peaks,
            },
            "worker_boundary": {"resources": dict(peaks)},
        }

    def test_same_four_containers_and_bounded_resources_are_accepted(self):
        validate_observations(self.value, self.containers)

    def test_extra_task_container_is_rejected(self):
        self.value["runtime_observations"]["phases"][3]["containers"].append("a" * 64)
        with self.assertRaises(CompactEvidenceError):
            validate_observations(self.value, self.containers)

    def test_changed_runtime_container_is_rejected(self):
        self.value["runtime_observations"]["phases"][3]["containers"][0] = "a" * 64
        with self.assertRaises(CompactEvidenceError):
            validate_observations(self.value, self.containers)

    def test_missing_idle_service_is_rejected(self):
        self.value["runtime_observations"]["idle"].pop()
        with self.assertRaises(CompactEvidenceError):
            validate_observations(self.value, self.containers)

    def test_nonfinite_resource_percentage_is_rejected(self):
        self.value["runtime_observations"]["idle"][0]["CPUPerc"] = "NaN%"
        with self.assertRaises(CompactEvidenceError):
            validate_observations(self.value, self.containers)

    def test_changed_peak_observation_is_rejected(self):
        self.value["runtime_observations"]["worker_peak"]["pids_peak"] = "999"
        with self.assertRaises(CompactEvidenceError):
            validate_observations(self.value, self.containers)
