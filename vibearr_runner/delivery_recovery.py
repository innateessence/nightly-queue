"""Explicit delivery-only continuation from retained original run facts."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from uuid import UUID, uuid4

from .journal import Journal, JournalError
from .native_resources import NativeResources
from .native_validation import CandidateSource
from .queue_acceptance import client
from .workspaces import Workspace, git, private_directory


def retained_workspace(directory, run_id, expected_head):
    root = directory / "workspaces" / str(UUID(run_id))
    if not root.exists() or root.is_symlink():
        raise JournalError("Recovery requires the retained original workspace.")
    root = private_directory(root)
    path = root / "workspace.json"
    if path.is_symlink() or not path.exists():
        raise JournalError(
            "Recovery requires the retained original workspace metadata."
        )
    saved = json.loads(path.read_text())
    if (
        saved.get("workspace_id") != run_id
        or Path(saved.get("directory", "")).resolve() != root
    ):
        raise JournalError("Recovery workspace belongs to another original run.")
    workspace = Workspace(**{**saved, "directory": root})
    with workspace.locked():
        workspace.verify()
        if git(["rev-parse", "HEAD"], workspace.checkout) != expected_head:
            raise JournalError(
                "Recovery checkout differs from the original delivered head."
            )
        if git(["status", "--porcelain", "--untracked-files=all"], workspace.checkout):
            raise JournalError("Recovery checkout contains uncommitted changes.")
        source = CandidateSource.from_git(workspace.checkout, expected_head)
    resources = NativeResources(directory)
    if not resources.path(run_id).exists():
        raise JournalError("Recovery requires original protected resource references.")
    references = resources.load(run_id)
    if (
        not references.get("instance_id")
        or references.get("pending")
        or references.get("pending_builds")
    ):
        raise JournalError("Recovery resource ownership is incomplete or unresolved.")
    return workspace, source


def restore(queue, journal, state, *, run_id, expected, execute=False):
    run_id = str(UUID(run_id))
    pending = state.get("delivery_recovery_request") or state.get(
        "delivery_recoveries", {}
    ).get(run_id)
    if pending and (pending["run_id"] != run_id or pending["expected"] != expected):
        raise JournalError("Reconcile the original delivery recovery request first.")
    if state.get("native"):
        if (
            pending
            and pending.get("restored")
            and state["native"].get("run_id") == run_id
        ):
            return {"run_id": run_id, "generation": 1, "restored": True}
        raise JournalError("An existing native operation owns this journal.")
    if state.get("run") or state.get("operation") or state.get("request"):
        raise JournalError(
            "Recovery requires an idle, fully reconciled controller journal."
        )
    if not any(
        r.get("run_id") == run_id
        and r.get("finish_digest") == expected["expected_finish_digest"]
        for r in state.get("completion_receipts", [])
    ):
        raise JournalError(
            "Recovery lacks the original positive completion acknowledgement."
        )
    gate = state["gates"]
    checkpoint = gate.get("checkpoint") or {}
    interval = checkpoint.get("interval") or {}
    interval_id = interval.get("id")
    if (
        gate.get("owned_run_id")
        or gate.get("pending_claim")
        or checkpoint.get("run_id") != run_id
        or not interval_id
        or gate.get("counts", {}).get(interval_id, 0) < 1
    ):
        raise JournalError(
            "Recovery cannot reconstruct the original counted claim interval."
        )
    workspace, source = retained_workspace(
        journal.directory, run_id, expected["expected_head_sha"]
    )
    remote = queue.request("GET", f"/api/v1/runner/runs/{run_id}/")
    if remote["run"]["runner_id"] != state["runner_id"]:
        raise JournalError("Recovery run belongs to another controller identity.")
    if not execute:
        return {
            "run_id": run_id,
            "generation": 1,
            "preview": True,
            "head_sha": source.commit_sha,
            "tree_sha": source.tree_sha,
            "counted_interval": interval_id,
            "tasks_started": gate["counts"][interval_id],
        }
    if pending is None:
        pending = {"run_id": run_id, "operation_id": str(uuid4()), "expected": expected}
        state["delivery_recovery_request"] = pending
        journal.save(state)
    response = queue.request(
        "POST",
        f"/api/v1/runner/runs/{run_id}/native/delivery-recovery/",
        {"operation_id": pending["operation_id"], **expected},
    )
    dto = response["delivery_recovery"]
    native = dto["native"]
    candidate = native["candidate"]
    if (
        dto["operation_id"] != pending["operation_id"]
        or dto["generation"] != 1
        or response["run"]["id"] != run_id
        or response["run"]["state"] != "active"
        or candidate["workspace"]["commit_sha"] != source.commit_sha
        or candidate["workspace"]["tree_sha"] != source.tree_sha
        or candidate["source_digest"] != source.digest
        or native["delivery"]["publication_id"] != expected["expected_publication_id"]
        or workspace.branch_name != candidate["workspace"]["branch"]
        or workspace.repository_url != native["repository"]["repository_url"]
    ):
        raise JournalError(
            "Server recovery facts differ from the retained original workspace."
        )
    native["workspace"] = {**asdict(workspace), "directory": str(workspace.directory)}
    state["native"] = native
    state["run"] = {"run": response["run"], "item": response["item"]}
    state["remote_active_run_id"] = run_id
    gate["owned_run_id"] = run_id
    gate.setdefault("attempts", {})[run_id] = interval_id
    gate.setdefault("attempt_accounts", {})[run_id] = [
        {"backend": r["backend"], "credential_id": r["credential_id"]}
        for r in checkpoint.get("readings", [])
    ]
    pending["restored"] = True
    pending["archive"] = dto["archive"]
    state.setdefault("delivery_recoveries", {})[run_id] = pending
    state.pop("delivery_recovery_request", None)
    journal.save(state)
    return {"run_id": run_id, "generation": 1, "restored": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-finish-digest", required=True)
    parser.add_argument("--expected-publication-id", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    expected = {
        "expected_finish_digest": args.expected_finish_digest,
        "expected_publication_id": args.expected_publication_id,
        "expected_head_sha": args.expected_head_sha,
    }
    with Journal(args.state) as journal:
        state = journal.load()
        queue = client()
        queue.controller_secret = state.get("controller_secret")
        print(
            json.dumps(
                restore(
                    queue,
                    journal,
                    state,
                    run_id=args.run_id,
                    expected=expected,
                    execute=args.execute,
                )
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
