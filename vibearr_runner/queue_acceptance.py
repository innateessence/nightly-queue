"""Explicit real queue acceptance; all work uses the normal controller."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .client import APIError, AuthenticationError, Client
from .controller import Controller, OwnershipLost
from .environment import getenv
from .journal import Journal, JournalError
from .maintenance import Maintenance
from .native_workspace import atomic_bytes
from .recovery import ReconciliationRequired


WAYPOINTS = {"agent-completed", "pushed", "pr-verified", "reporting-pending"}


def preserve_interruption(directory, stdout, stderr, returncode):
    def bounded(value):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = value or ""
        for key, secret in os.environ.items():
            sensitive = any(
                part in key.upper() for part in ("TOKEN", "PASSWORD", "SECRET", "KEY")
            )
            if sensitive and len(secret) >= 8:
                value = value.replace(secret, "[redacted]")
        return value[-65536:]

    record = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "returncode": returncode,
        "stdout": bounded(stdout),
        "stderr": bounded(stderr),
        "resume": "Keep the same private journal and reconcile the original operations.",
    }
    atomic_bytes(
        directory / "queue-acceptance-last-interruption.json",
        json.dumps(record, sort_keys=True).encode(),
    )


def client():
    return Client(
        getenv("NQ_URL", "http://vibearr:8000"),
        getenv("NQ_RUNNER_API_TOKEN", ""),
        max_bytes=8388608,
    )


def summary(state, result, observations=None):
    owned = state.get("run") or {}
    run = owned.get("run", {})
    item = owned.get("item") or {}
    native = state.get("native", {})
    delivery = native.get("delivery", {})
    waypoint = "agent-completed" if native.get("phase") == "import_candidate" else ""
    checkpoints = {
        "pushed": "pushed",
        "pr_verified": "pr-verified",
        "reporting_pending": "reporting-pending",
    }
    waypoint = checkpoints.get(delivery.get("checkpoint"), waypoint)
    return {
        "schema_version": 1,
        "pid": os.getpid(),
        "result": result,
        "run_id": run.get("id"),
        "run_state": run.get("state"),
        "issue_number": item.get("issue_number"),
        "project_id": item.get("project", {}).get("id"),
        "waypoint": waypoint,
        "completion_receipts": state.get("completion_receipts", [])[-1:],
        "executor_observations": observations or [],
    }


def cycle_once(directory, project_id, issue_number):
    queue = client()
    with Journal(directory) as journal:
        state = journal.load()
        queue.controller_secret = state.get("controller_secret")
        maintenance = Maintenance(queue, journal, directory / "workspaces")
        controller = Controller(
            queue, journal, maintenance=maintenance, completed=maintenance.acknowledge
        )
        controller.state = state
        try:
            configuration = controller.probe(state)
            projects = [entry["project_id"] for entry in configuration["projects"]]
            if projects != [project_id]:
                raise ValueError(
                    "Acceptance requires a dedicated configuration containing only its selected project."
                )
            owned = state.get("run") or {}
            if owned and owned["run"]["state"] == "active":
                item = owned["item"]
                if (
                    item.get("project", {}).get("id") != project_id
                    or item.get("issue_number") != issue_number
                ):
                    controller.suspend_owned(state, ownership_lost=True)
                    raise ValueError(
                        "The owned claim differs from the explicitly selected acceptance issue."
                    )
            result = controller.cycle(state, configuration)
        except (AuthenticationError, OwnershipLost):
            controller.suspend_owned(state, ownership_lost=True)
            raise
        except APIError:
            controller.suspend_owned(state)
            raise
        except JournalError as exc:
            controller.suspend_owned(
                state, ownership_lost=not isinstance(exc, ReconciliationRequired)
            )
            raise
        observations = []
        native = state.get("native", {})
        session = native.get("agent", {}).get("session")
        validation = native.get("validation", {}).get("session")
        manifests = [session["manifest"]] if session else []
        if validation:
            manifests.extend(entry["manifest"] for entry in validation["operations"])
        if manifests:
            from vibearr_executor.client import Client as ExecutorClient

            executor = ExecutorClient.from_environment()
            for manifest in manifests:
                observed = executor.status(manifest["operation_id"])
                if observed.get("state") != "running":
                    continue
                observations.append(
                    {
                        "operation_id": manifest["operation_id"],
                        "run_id": manifest["run_id"],
                        "container_id": observed.get("container_id"),
                        "state": observed["state"],
                        "phase": manifest["phase"],
                        "pid": os.getpid(),
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        return summary(state, result, observations)


def run_acceptance(
    directory,
    project_id,
    issue_number,
    evidence,
    max_seconds,
    *,
    progress_name="queue-acceptance.json",
    expected_outcome="delivered",
):
    if Path(progress_name).name != progress_name or not progress_name.endswith(".json"):
        raise ValueError(
            "Acceptance progress must be a JSON filename beneath its private state directory."
        )
    if expected_outcome not in {"delivered", "failed"}:
        raise ValueError("Choose delivered or failed acceptance outcome.")
    with Journal(directory):
        progress_file = directory / progress_name
        saved = (
            json.loads(progress_file.read_text())
            if progress_file.exists()
            else {
                "schema_version": 1,
                "project_id": project_id,
                "issue_number": issue_number,
                "run_id": None,
                "observed": {},
                "generations": 0,
            }
        )
    if saved["project_id"] != project_id or saved["issue_number"] != issue_number:
        raise ValueError(
            "Acceptance progress belongs to another explicitly selected task."
        )
    deadline = time.monotonic() + max_seconds
    command = [
        sys.executable,
        "-m",
        "vibearr_runner.queue_acceptance",
        "--execute",
        "--cycle",
        "--state",
        str(directory),
        "--project-id",
        str(project_id),
        "--issue-number",
        str(issue_number),
    ]
    observed, run_id, generations = (
        saved["observed"],
        saved["run_id"],
        saved["generations"],
    )
    executor_observations = saved.get("executor_observations", {})
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=max(1, deadline - time.monotonic()),
            )
        except subprocess.TimeoutExpired as exc:
            preserve_interruption(directory, exc.stdout, exc.stderr, None)
            raise RuntimeError(
                "Acceptance cycle timed out; retain private interruption diagnostics and resume the same journal."
            ) from None
        if result.returncode:
            preserve_interruption(
                directory, result.stdout, result.stderr, result.returncode
            )
            raise RuntimeError(
                "Acceptance supervisor cycle was interrupted; inspect private interruption diagnostics and resume the same journal."
            )
        try:
            progress = json.loads(result.stdout)
        except ValueError:
            preserve_interruption(
                directory, result.stdout, result.stderr, result.returncode
            )
            raise RuntimeError(
                "Acceptance cycle readback was invalid; reconcile the same private journal."
            ) from None
        generations += 1
        for operation in progress.get("executor_observations", []):
            previous = executor_observations.setdefault(operation["operation_id"], [])
            if previous and operation["container_id"] != previous[0]["container_id"]:
                raise RuntimeError(
                    "Supervisor restart replaced the original running operation container."
                )
            if len(previous) < 2:
                previous.append(operation)
        if progress.get("run_id"):
            if run_id not in {None, progress["run_id"]}:
                raise RuntimeError(
                    "Acceptance observed a replacement run; retain original ownership."
                )
            run_id = progress["run_id"]
        waypoint = progress.get("waypoint")
        if waypoint and waypoint not in observed:
            observed[waypoint] = {
                "generation": generations,
                "pid": progress["pid"],
                "observed_at": datetime.now(timezone.utc).isoformat(),
            }
            print(
                json.dumps(
                    {"run_id": run_id, "waypoint": waypoint, "generation": generations}
                ),
                flush=True,
            )
        saved.update(
            run_id=run_id,
            observed=observed,
            generations=generations,
            executor_observations=executor_observations,
        )
        atomic_bytes(progress_file, json.dumps(saved, sort_keys=True).encode())
        if progress["result"] == "completed":
            return evidence_document(
                directory,
                run_id,
                project_id,
                issue_number,
                observed,
                generations,
                evidence,
                expected_outcome=expected_outcome,
                executor_observations=executor_observations,
            )
        time.sleep(0.1 if progress["result"] in {"continue", "claimed", "saved"} else 2)
    raise RuntimeError(
        "Acceptance time limit elapsed; retain the original journal and operation IDs."
    )


def evidence_document(
    directory,
    run_id,
    project_id,
    issue_number,
    observed,
    generations,
    evidence,
    *,
    expected_outcome="delivered",
    executor_observations=None,
):
    required = WAYPOINTS if expected_outcome == "delivered" else {"agent-completed"}
    if run_id is None or not required <= set(observed):
        raise RuntimeError(
            "Acceptance did not observe every required restart checkpoint."
        )
    with Journal(directory) as journal:
        state = journal.load()
    queue = client()
    queue.controller_secret = state["controller_secret"]
    response = queue.reconcile(run_id)
    run, item = response["run"], response["item"]
    expected_status = "succeeded" if expected_outcome == "delivered" else "failed"
    if (
        run["state"] != "completed"
        or run["result_status"] != expected_status
        or run.get("native_outcome") != expected_outcome
    ):
        raise RuntimeError("Acceptance did not finish with verified native delivery.")
    if item["project"]["id"] != project_id or item["issue_number"] != issue_number:
        raise RuntimeError("Acceptance result belongs to another selected issue.")
    delivery = next(
        (
            value["data"]
            for value in run["evidence"]
            if value["kind"] == "verified_delivery"
        ),
        None,
    )
    if expected_outcome == "delivered" and delivery is None:
        raise RuntimeError(
            "Acceptance completion lacks historical version2 delivery readback."
        )
    document = {
        "schema_version": 1,
        "run_id": run_id,
        "project_id": project_id,
        "issue_number": issue_number,
        "supervisor_generations": generations,
        "restarts": observed,
        "finish_digest": run["finish_digest"],
        "delivery": delivery,
        "outcome": expected_outcome,
        "executor_observations": executor_observations or {},
        "completed_at": run["finished_at"],
        "reporting_acknowledged": any(
            value["run_id"] == run_id for value in state.get("completion_receipts", [])
        ),
    }
    atomic_bytes(evidence, json.dumps(document, sort_keys=True, indent=2).encode())
    return {
        "run_id": run_id,
        "pull_request": run["result_url"],
        "evidence": str(evidence),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Explicit real issue-to-PR acceptance through the normal native controller."
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Authorize the configured controller's real model and GitHub operations.",
    )
    parser.add_argument("--cycle", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--issue-number", type=int, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--max-seconds", type=int, default=3600)
    arguments = parser.parse_args()
    if (
        not arguments.execute
        or not 0 < arguments.project_id < 2**63
        or not 0 < arguments.issue_number < 2**63
    ):
        parser.error(
            "Select positive project/issue IDs and explicitly enable real acceptance with --execute."
        )
    if not arguments.cycle and (
        arguments.evidence is None or not 60 <= arguments.max_seconds <= 7200
    ):
        parser.error(
            "Choose an evidence path and a bounded acceptance duration between 60 and 7200 seconds."
        )
    try:
        result = (
            cycle_once(arguments.state, arguments.project_id, arguments.issue_number)
            if arguments.cycle
            else run_acceptance(
                arguments.state,
                arguments.project_id,
                arguments.issue_number,
                arguments.evidence,
                arguments.max_seconds,
            )
        )
    except (
        APIError,
        JournalError,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            "Queue acceptance stopped: "
            + type(exc).__name__
            + ". Preserve the private journal and reconcile original operations.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
