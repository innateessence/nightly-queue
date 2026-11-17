"""Operator qualification client; receives no container-engine access."""

import json
import os
from pathlib import Path
import sys
import time

from vibearr_executor.client import Client, ExecutorUnavailable
from vibearr_executor.contracts import canonical
from vibearr_executor.runtime import durable
from vibearr_executor.validation_profile import ValidationProfile


ROOT = Path("/run/slots")


def clients():
    return {
        phase: Client(
            f"http://{phase}-worker:8791",
            (ROOT / phase / "control-token").read_text().strip(),
            timeout=180,
        )
        for phase in ("agent", "setup", "validation")
    }


def main():
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) == 2 else ""
    if action not in {"prepare", "finish"}:
        raise SystemExit("Choose prepare or finish qualification.")
    workers = clients()
    if action == "prepare":
        for phase, client in workers.items():
            witness = client.request("/v2/qualification/restart", {})
            print(json.dumps({"worker": phase, "restart_witness": witness}), flush=True)
        return
    reports = {}
    for phase, client in workers.items():
        deadline = time.monotonic() + 600
        endpoint = "/v2/qualification/run"
        while True:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Worker qualification did not finish within ten minutes."
                )
            try:
                reports[phase] = client.request(endpoint, {})
                if reports[phase].get("state") == "completed":
                    break
                if reports[phase].get("state") != "running":
                    raise RuntimeError(
                        "Qualification did not complete; prepare a new restart witness."
                    )
                endpoint = "/v2/qualification/status"
                time.sleep(1)
            except ExecutorUnavailable as error:
                if error.http_status in {400, 409} or time.monotonic() >= deadline:
                    raise
                endpoint = "/v2/qualification/status"
                time.sleep(1)
        if not client.capabilities()["qualified"]:
            raise RuntimeError("Worker did not seal current boot qualification.")
        print(
            json.dumps({"worker": phase, "checks": reports[phase]["checks"]}),
            flush=True,
        )
    gateway = Client(
        "http://worker-gateway:8791",
        Path("/run/vibearr/runner/control-token").read_text().strip(),
    )
    capabilities = gateway.capabilities()
    if not capabilities["qualified"]:
        raise RuntimeError("Gateway did not recognize complete worker qualification.")
    image = workers["validation"].capabilities()["profiles"][0]["image"]
    database = workers["validation"].capabilities()["profiles"]
    database = next(
        profile["database_image"] for profile in database if profile["database_image"]
    )
    from vibearr_executor.contracts import digest

    requirements = Path("/app/requirements.txt").read_bytes()
    profile = ValidationProfile(image, database, digest(requirements))
    durable(
        Path("/run/vibearr/runner/validation-profile.json"), canonical(profile.data())
    )
    report = {
        "schema_version": 1,
        "runtime": "compose-slot-v2",
        "binding_digest": capabilities["binding_digest"],
        "workers": reports,
        "qualified": True,
    }
    durable(Path("/run/vibearr/runner/docker-qualification.json"), canonical(report))
    print(
        json.dumps(
            {"qualified": True, "binding_digest": capabilities["binding_digest"]}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
