"""Protected build termination is attested only after original resource cleanup."""

from .contracts import canonical, digest
from .engine import EngineError


def confirm(builds, record):
    build_id = record["request"]["build_id"]
    if record["state"] not in {"ready", "canceled"}:
        raise EngineError(
            "Only ready or reconciled canceled builds can attest termination."
        )
    setup = builds.runtime.termination_receipt(build_id)
    if not setup["resources_terminated"]:
        raise EngineError("Setup resources remain uncertain; retain the build fence.")
    proof_state = builds.path(build_id) / "qualification"
    if builds.processes(proof_state):
        raise EngineError(
            "Qualification resources remain active; retain the build fence."
        )
    builds.cleanup_qualification(record)
    if record.get("assembly_intent"):
        builds.cleanup_assembly(record)
    receipt = {
        "schema_version": 1,
        "instance_id": builds.runtime.instance,
        "run_id": record["request"]["run_id"],
        "build_id": build_id,
        "state": record["state"],
        "resources_terminated": True,
        "setup_receipt": setup,
        "assembly_container_id": record.get("assembly_container_id"),
        "assembly_absent": True,
        "qualification_cleanup_confirmed": True,
    }
    record["termination_receipt"] = receipt
    record["receipt_digest"] = digest(canonical(receipt))
    builds.save(record)
    return builds.public(record)


def read(builds, build_id):
    with builds.runtime.lock:
        record = builds.read(build_id)
        if record["state"] in {"ready", "canceled"} and not record.get(
            "termination_receipt"
        ):
            return confirm(builds, record)
        return builds.public(record)
