"""Exercise real task UIDs, containment and fresh-job reset in a Docker slot."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import threading
import time
from uuid import uuid4

from .artifacts import pack, unpack
from .contracts import Limits, Manifest, Profile, canonical, digest
from .slot_network import configure
from .slot_safety import binding
from .slots import SlotRuntime


ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.slot_probe")


def wait(runtime, identity):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        observed = runtime.status(identity)
        if observed["state"] in {"exited", "unknown"}:
            return observed
        time.sleep(0.05)
    raise RuntimeError("Worker did not produce a bounded terminal result.")


def main():
    configure([])
    image = os.environ["VIBEARR_SLOT_IMAGE_ID"]
    limits = Limits(2, 4096, 128, 2048)
    plain = Profile("boundary-probe", 1, image, "validation", ARGV, limits)
    credentialed = Profile(
        "credential-probe",
        1,
        image,
        "agent",
        ARGV,
        limits,
        backend_secret_env="PROBE_CREDENTIAL",
    )
    runtime = SlotRuntime(Path("/control"), [plain, credentialed])
    runtime.qualifying = True
    binding(runtime)
    # A real listener distinguishes UID firewall denial from an absent service.
    listener = socket.socket()
    listener.bind(("0.0.0.0", 9001))
    listener.listen()
    listener.settimeout(0.1)
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
                connection.close()
            except TimeoutError:
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    checks = []
    try:

        def prepare(case, *, seconds=20, secret=""):
            profile = credentialed if secret else plain
            source = pack({"payload.txt": (0o644, b"fresh candidate\n")})
            context = canonical({"case": case})
            for kind, content in (("source", source), ("context", context)):
                runtime.upload(kind, content, digest(content))
            manifest = Manifest(
                1,
                str(uuid4()),
                str(uuid4()),
                profile.profile_id,
                profile.digest,
                digest(source),
                digest(context),
                profile.phase,
                limits,
                (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(),
                (),
            )
            runtime.prepare(manifest.data())
            if secret:
                runtime.supply_secret(manifest.operation_id, secret)
            return manifest.operation_id

        for _ in range(2):
            identity = prepare("smoke")
            runtime.start(identity)
            result = wait(runtime, identity)
            assert result["state"] == "exited", result
            assert result["result"]["exit_code"] == 0, runtime.logs(identity, 0, 65536)
            assert result["descendants_terminated"] is True
            assert result["result"]["log_digest"] == digest(
                runtime.logs(identity, 0, 65536)
            )
            _, content = runtime.export(identity)
            assert unpack(content)["payload.txt"][1] == b"verified task change\n"
        checks.extend(
            [
                "task-uid-and-capabilities",
                "private-control-and-image-denial",
                "network-denial",
                "escaped-descendant-stop",
                "cross-job-home-tmp-shm-ipc-reset",
                "verified-source-export",
            ]
        )
        identity = prepare("wait", seconds=3)
        runtime.start(identity)
        result = wait(runtime, identity)
        assert (
            result["result"]["timed_out"] is True
            and result["descendants_terminated"] is True
        )
        checks.append("deadline-termination")
        identity = prepare("wait")
        runtime.start(identity)
        time.sleep(0.3)
        result = runtime.stop(identity)
        assert result["state"] == "exited" and result["result"]["canceled"] is True
        checks.append("confirmed-cancellation")
        identity = prepare("wait")
        runtime.start(identity)
        time.sleep(0.3)
        runtime.pause(identity)
        from .slot_processes import task_states

        assert all(state in {"T", "t", "Z"} for state in task_states().values())
        runtime.resume(identity)
        runtime.stop(identity)
        checks.append("escaped-descendant-pause-resume")
        secret = "qualification-only-synthetic-key-0123456789"
        identity = prepare("credential", secret=secret)
        runtime.start(identity)
        result = wait(runtime, identity)
        assert secret.encode() not in runtime.logs(identity, 0, 65536)
        assert result["result"].get("export_error") and not result["result"].get(
            "artifact"
        )
        checks.append("credential-redaction-and-export-rejection")
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime": "compose-slot-v2",
                    "checks": checks,
                    "qualified": False,
                    "reason": "Foundation probes passed; full deployment qualification remains required.",
                },
                indent=2,
            )
        )
    finally:
        runtime.close()
        stop.set()
        thread.join(timeout=1)
        listener.close()


if __name__ == "__main__":
    main()
