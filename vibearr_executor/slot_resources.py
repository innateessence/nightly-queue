"""Exercise kernel OOM enforcement in a small disposable copy of the worker."""

import json
import os
from pathlib import Path

from .contracts import Limits, Profile
from .slot_acceptance import ARGV
from .slot_network import configure
from .slot_qualification import passed, run_case
from .slot_safety import binding
from .slots import SlotRuntime


def oom_kills():
    events = dict(
        line.split()
        for line in Path("/sys/fs/cgroup/memory.events").read_text().splitlines()
    )
    return int(events["oom_kill"])


def main():
    cgroup = Path("/sys/fs/cgroup")
    print(
        json.dumps(
            {
                "kind": "memory-probe-starting",
                "memory_bytes": (cgroup / "memory.max").read_text().strip(),
                "current_bytes": (cgroup / "memory.current").read_text().strip(),
                "oom_group": (cgroup / "memory.oom.group").read_text().strip(),
                "oom_score_adj": Path("/proc/self/oom_score_adj").read_text().strip(),
            }
        ),
        flush=True,
    )
    configure([])
    profile = Profile(
        "memory-boundary-probe",
        1,
        os.environ["VIBEARR_SLOT_IMAGE_ID"],
        "validation",
        ARGV,
        Limits(2, 384, 128, 32),
    )
    runtime = SlotRuntime(Path("/control"), [profile])
    try:
        observed_binding = binding(runtime)
        before = oom_kills()
        operation, result = run_case(runtime, "memory")
        if (
            result["state"] != "exited"
            or result["result"]["exit_code"] != -9
            or result.get("descendants_terminated") is not True
            or result["result"].get("timed_out")
            or oom_kills() <= before
        ):
            raise RuntimeError("Kernel OOM termination and cleanup were not verified.")
        if b'"memory_exhaustion_started": true' not in runtime.logs(
            operation, 0, 65536
        ):
            raise RuntimeError("The fixed task did not begin the memory probe.")
        operation, result = run_case(runtime, "smoke")
        passed(runtime, operation, result)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "disposable-memory-enforcement",
                    "binding": observed_binding,
                    "oom_kills": oom_kills() - before,
                    "kernel_oom_enforced": True,
                    "slot_reused_after_oom": True,
                    "production_fleet_qualification": False,
                }
            ),
            flush=True,
        )
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
