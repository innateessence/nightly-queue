"""Export selected protected model-free receipts without private control material."""

import json
import os
from pathlib import Path

from .contracts import canonical, digest
from .client import Client
from .control_token import read_control_token
from .runtime import durable


ROOT = Path("/control")
FIELDS = (
    "manifest",
    "operation_id",
    "state",
    "descendants_terminated",
    "result",
    "worker_boot",
    "execution_identity",
    "receipt_digest",
)


def operation(identity):
    from .contracts import identifier

    value = json.loads(
        (ROOT / "operations" / identifier(identity) / "record.json").read_bytes()
    )
    return {key: value[key] for key in FIELDS}


def oom_operation(records):
    context = digest(canonical({"case": "memory"}))
    selected = [
        record
        for record in records
        if record["manifest"]["profile_id"] == "worker-qualification"
        and record["manifest"]["phase"] == "agent"
        and record["manifest"]["context_digest"] == context
        and record["result"]["exit_code"] == -9
    ]
    if len(selected) != 1:
        raise ValueError(
            "Expected exactly one actual memory probe in the current qualification."
        )
    return selected[0]


def main():
    output = ROOT / "engineering-proofs"
    output.mkdir(mode=0o700, exist_ok=False)
    boundary = json.loads((ROOT / "compact-qualification.json").read_bytes())
    token = read_control_token(Path(os.environ["VIBEARR_EXECUTOR_TOKEN_FILE"]))
    capabilities = Client("http://127.0.0.1:8791", token).capabilities()
    durable(output / "capabilities.json", canonical(capabilities))
    durable(output / "worker-boundary.json", canonical(boundary))
    witness = json.loads((ROOT / "restart-witness.json").read_bytes())
    restart = operation(witness["operation_id"])
    durable(
        output / "worker-restart.json",
        canonical(
            {
                "operation": restart,
                "restart_receipt": restart["receipt_digest"],
                "boundary": boundary,
            }
        ),
    )
    records = [operation(value["operation_id"]) for value in boundary["receipts"]]
    durable(
        output / "resource-limits.json",
        canonical({"boundary": boundary, "oom_operation": oom_operation(records)}),
    )
    session = json.loads((ROOT / "candidate-check/session.json").read_bytes())
    report = json.loads((ROOT / "candidate-check/evidence.json").read_bytes())
    records = [
        operation(value["manifest"]["operation_id"]) for value in session["operations"]
    ]
    durable(
        output / "trusted-validation.json",
        canonical({"report": report, "operations": records}),
    )
    print(
        json.dumps(
            {
                "kind": "actual-protected-proof-export",
                "source_revision": report["commit_sha"],
                "worker_boot": boundary["worker_boot"],
            }
        )
    )


if __name__ == "__main__":
    main()
