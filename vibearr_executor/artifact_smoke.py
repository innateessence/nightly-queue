"""Exercise an installed executor service/client outside a source checkout."""

import argparse
import json
import sys
from pathlib import Path

from .artifacts import pack
from .client import Client, ExecutorRejected, ExecutorUnavailable
from .contracts import canonical
from .qualify import Harness, require, smoke_profiles


def smoke(state: Path, image: str) -> dict:
    package = Path(__file__).resolve().parent
    require(
        package.is_relative_to(Path(sys.prefix).resolve()),
        "Run this smoke with the installed executor package outside the source checkout.",
    )
    require(
        (package / "Dockerfile").is_file(),
        "Installed executor image recipe resource is missing",
    )
    harness = Harness(state, smoke_profiles(image))
    operation = None
    try:
        harness.start()
        try:
            Client(harness.client.url, "invalid-token-" * 4).capabilities()
        except ExecutorRejected as exc:
            require(
                exc.http_status == 401,
                "Installed service did not reject invalid authentication",
            )
        else:
            raise RuntimeError("Installed service accepted invalid authentication")
        source = pack(
            {"original.txt": (0o644, b"original\n"), "delete.txt": (0o644, b"delete\n")}
        )
        manifest = harness.prepare("validation", source)
        operation = manifest["operation_id"]
        harness.client.start(operation)
        result = harness.finished(operation)
        require(
            result["result"]["exit_code"] == 0 and result.get("descendants_terminated"),
            "Installed service did not complete its real OCI task",
        )
        inventory, exported = harness.client.export(operation, source)
        require(
            inventory["result_digest"] == result["result"]["artifact"]["result_digest"]
            and exported,
            "Installed client did not verify its result export",
        )
        require(
            "django" not in sys.modules, "Executor imported application dependencies"
        )
        return {
            "schema_version": 1,
            "installed_package": str(Path(__file__).resolve().parent),
            "operation_id": operation,
            "container_id": result["container_id"],
            "exit_code": 0,
            "descendants_terminated": True,
            "artifact": inventory,
            "log_digest": result["result"]["log_digest"],
            "native_agent_ready": False,
        }
    finally:
        if operation:
            try:
                harness.client.stop(operation)
            except ExecutorUnavailable:
                pass
        harness.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    result = smoke(args.state, args.image)
    if args.evidence:
        args.evidence.write_bytes(canonical(result) + b"\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
