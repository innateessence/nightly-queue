"""Explicit controlled live qualification; replay results never grant execution permission."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .client import Client, InvalidResponse
from .journal import Journal
from .sandbox import SandboxUnavailable, probe_sandbox, sandbox_command

TRANSPORT = "codex-responses-structured-proposals-v1"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def deployment_environment() -> dict[str, Any]:
    version = (
        subprocess.run(
            ["/usr/bin/bwrap", "--version"], capture_output=True, check=True, timeout=5
        )
        .stdout.decode()
        .strip()
    )
    policy = sandbox_command(Path("/BOUND_WORKSPACE"), ("/usr/bin/true",))
    return {
        "image_digest": os.environ.get("NQ_RUNNER_IMAGE_DIGEST", "unbound"),
        "kernel": platform.release(),
        "source_revision": os.environ.get("NQ_RUNNER_SOURCE_REVISION", "unbound"),
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "uid": os.getuid(),
        "bubblewrap": version,
        "sandbox_policy": policy,
        "operator_policy": os.environ.get(
            "NQ_RUNNER_SANDBOX_POLICY_DIGEST", "default-deny"
        ),
    }


def deployment_binding() -> dict[str, str]:
    directory = Path(__file__).resolve().parent
    files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.py"))
        if not path.name.startswith("test_")
    }
    with tempfile.TemporaryDirectory() as temporary:
        checkout = Path(temporary)
        probe_sandbox(checkout)
    environment = deployment_environment()
    return {"code_revision": digest(files), "environment_digest": digest(environment)}


def matches_deployment(proof: dict[str, Any]) -> bool:
    if proof.get("qualified") is not True:
        return False
    try:
        expires = datetime.fromisoformat(proof["expires_at"])
        binding = deployment_binding()
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
        SandboxUnavailable,
    ):
        return False
    return (
        expires.tzinfo is not None
        and expires > datetime.now(UTC)
        and all(proof.get(key) == value for key, value in binding.items())
    )


class ControlledRelease:
    def __init__(
        self,
        client: Client,
        journal: Journal,
        qualification_id: str,
        *,
        mode: str = "live",
        binding=None,
    ):
        if mode not in {"live", "replay"}:
            raise ValueError("Use live or replay mode.")
        self.client, self.journal = client, journal
        self.qualification_id = str(UUID(qualification_id))
        self.path = f"/api/v1/runner/release/{self.qualification_id}/"
        self.mode = mode
        self.binding = binding or deployment_binding
        self.boot_id = str(uuid4())

    def authorization(self):
        result = self.client.request("GET", self.path)
        entry = result.get("qualification", {})
        binding = self.binding()
        if result.get("schema_version") != 1 or any(
            entry.get(key) != value for key, value in binding.items()
        ):
            raise InvalidResponse(
                "Controlled release does not match the deployed code and sandbox."
            )
        if datetime.fromisoformat(entry["expires_at"]) <= datetime.now(UTC):
            raise InvalidResponse("Controlled release authorization expired.")
        return entry

    def recover(self, state, gates, *, execution=True):
        from .client import NotFoundError
        from .processes import ProcessTracker
        from .recovery import RecoveryCoordinator, ReconciliationRequired

        ProcessTracker(self.journal, state).recover()
        recovery = RecoveryCoordinator(self.client, self.journal, gates)
        recovery.bind(state)
        if (state.get("operation") or {}).get("kind") == "claim":
            raise ReconciliationRequired(
                "Controlled recovery cannot repeat an ordinary queue claim."
            )
        pending = state.get("release_claim")
        if pending:
            if state.get("run") or not execution:
                raise ReconciliationRequired(
                    "Resolve the controlled claim before other work."
                )
            pending_id = str(UUID(pending["run_id"]))
            try:
                remote = self.client.reconcile(pending_id)
            except NotFoundError:
                active = self.client.active().get("run")
                if active or state.get("remote_active_run_id"):
                    raise ReconciliationRequired(
                        "Unknown owned work conflicts with the controlled claim."
                    ) from None
                # The original HTTP request may still commit after this lookup.
                return None
            else:
                entry = self.authorization()
                item = remote.get("item") or {}
                if (
                    remote["run"]["id"] != pending_id
                    or remote["run"]["state"] != "active"
                    or item.get("id") != pending["item_id"]
                    or item.get("repository_url") != entry["repository_url"]
                ):
                    raise ReconciliationRequired(
                        "Controlled claim recovery is outside its exact authorized item."
                    )
                self.claim(state, pending["item_id"], confirmed_existing=True)
        local = state.get("run")
        if not local and state.get("gates", {}).get("pending_claim"):
            raise ReconciliationRequired(
                "A pending claim lacks its controlled authorization intent."
            )
        active = self.client.active().get("run")
        state["remote_active_run_id"] = active["id"] if active else None
        if local and execution:
            if (
                local.get("item", {}).get("repository_url")
                != self.authorization()["repository_url"]
            ):
                raise ReconciliationRequired(
                    "Owned work is outside the designated controlled repository."
                )
        return recovery.reconcile(state)

    def claim(self, state, item_id: int, *, confirmed_existing=False):
        self.authorization()
        if state.get("run") or state.get("operation"):
            raise InvalidResponse(
                "Recover existing owned work before a controlled claim."
            )
        from .backend_registry import BackendRegistry, ScopedCredentials
        from .gates import GateEvaluator
        from .maintenance import MINIMUM_FREE_BYTES
        import shutil

        if (
            not confirmed_existing
            and shutil.disk_usage(self.journal.directory).free < MINIMUM_FREE_BYTES
        ):
            raise InvalidResponse("Disk pressure blocks new controlled claims.")
        gates = GateEvaluator(self.journal)
        pending = state.get("release_claim")
        if pending and pending["item_id"] != item_id:
            raise InvalidResponse("Reconcile the designated controlled claim first.")
        if confirmed_existing and not pending:
            raise InvalidResponse(
                "Controlled claim recovery requires its original durable intent."
            )
        if not confirmed_existing:
            configuration = self.client.configuration()
            configuration["execution_ready"] = True
            gates.adapters.update(
                BackendRegistry().usage_adapters(
                    configuration["accounts"], ScopedCredentials(self.client)
                )
            )
            decision = gates.evaluate(
                configuration,
                state,
                checkpoint="claim",
                retry_claim_id=pending["run_id"] if pending else None,
            )
            self.client.gate_report(decision.data())
            if not decision.allowed:
                raise InvalidResponse(
                    "Real schedule, pause, capacity, or usage gates block the controlled claim."
                )
        if not pending:
            pending = {"run_id": str(uuid4()), "item_id": item_id}
            state["release_claim"] = pending
            gates.prepare_claim(state, pending["run_id"], decision)
        response = self.client.request("POST", self.path + "claim/", pending)
        if response["item"]["repository_url"] != self.authorization()["repository_url"]:
            raise InvalidResponse(
                "Controlled claim is outside the authorized repository."
            )
        state["run"] = response
        state["remote_active_run_id"] = response["run"]["id"]
        state["release_claim"] = None
        gates.record_claim(state, response["run"]["id"])
        self.journal.save(state)
        return response

    def step(self, state, lifecycle):
        entry = self.authorization()
        run = state.get("run") or {}
        if run.get("item", {}).get("repository_url") != entry["repository_url"]:
            raise InvalidResponse(
                "Controlled lifecycle requires the designated owned test run."
            )
        return lifecycle.step(state)

    def candidate_readiness(self, state, account, capabilities, probe):
        from .backends import BackendReadiness

        entry = self.authorization()
        run = state.get("run") or {}
        if run.get("item", {}).get("repository_url") != entry["repository_url"]:
            raise InvalidResponse(
                "Controlled authentication probe is outside the authorized owned run."
            )
        if run.get("run", {}).get("state") != "active":
            raise InvalidResponse(
                "Controlled authentication requires an active owned test run."
            )
        effective = (
            run.get("run", {}).get("configuration_snapshot", {}).get("effective", {})
        )
        matching = any(
            all(
                account.get(key) == effective.get(f"{stage}_{key}")
                for key in ("backend", "credential_id", "model")
            )
            for stage in ("research", "implementation")
        )
        if (
            not matching
            or capabilities.native_tools
            or capabilities.transport != "structured_proposals"
        ):
            raise InvalidResponse(
                "Controlled authentication is outside the selected stage or mediated transport."
            )
        model = account.get("model")
        if not isinstance(model, str) or not model or probe() != model:
            raise InvalidResponse(
                "The selected live backend model authentication probe failed."
            )
        return BackendReadiness(True, True, (), capabilities)

    def observe_pr(self, state, github):
        entry = self.authorization()
        delivery = state.get("delivery") or {}
        pull = delivery.get("pull_request") or {}
        observed = github.find(
            pull["repository"], pull["head"], pull["base"], pull["commit_sha"]
        )
        if observed != pull:
            raise InvalidResponse(
                "Live pull request identity changed during restart verification."
            )
        evidence = state.setdefault("release_evidence", {})
        observations = evidence.setdefault("pr_observations", [])
        observation = {
            "url": observed["url"],
            "commit_sha": observed["commit_sha"],
            "repository_url": entry["repository_url"],
            "head_branch": observed["head"],
            "matching_pr_count": 1,
            "controller_boot_id": self.boot_id,
        }
        if not observations or observations[-1]["controller_boot_id"] != self.boot_id:
            observations.append(observation)
            self.journal.save(state)
        return len(observations)

    def boundary(self, state, workflow_run_url, evidence_path):
        entry = self.authorization()
        if evidence_path.stat().st_size > 65536:
            raise InvalidResponse("Boundary evidence exceeds its limit.")
        evidence = json.loads(evidence_path.read_text())
        evidence["workflow_run_url"] = workflow_run_url
        for key in ("code_revision", "environment_digest"):
            if evidence.get(key) != entry[key]:
                raise InvalidResponse(
                    "Boundary artifact does not match this deployment authorization."
                )
        run = state.get("run") or {}
        grant = self.client.request(
            "POST",
            "/api/v1/runner/secrets/",
            {
                "scope": "run",
                "run_id": run["run"]["id"],
                "claim_token": run["item"]["claim_token"],
                "purpose": "repository",
            },
        )
        from .release_evidence import verify_boundary_workflow

        verification = verify_boundary_workflow(evidence, grant["material"]["secret"])
        state.setdefault("release_evidence", {})["boundary"] = evidence
        state["release_evidence"]["workflow"] = verification
        self.journal.save(state)
        return verification

    def record_result(self, state):
        from .maintenance import Maintenance
        from .gates import GateEvaluator

        run = state["run"]
        remote = self.client.reconcile(run["run"]["id"])["run"]
        if remote["state"] != "completed" or remote["result_status"] not in {
            "succeeded",
            "failed",
        }:
            raise InvalidResponse(
                "The scenario has not completed with durable reporting."
            )
        result = (
            "success_run_id"
            if remote["result_status"] == "succeeded"
            else "failure_run_id"
        )
        state.setdefault("release_evidence", {})[result] = remote["id"]
        Maintenance(
            self.client, self.journal, self.journal.directory / "workspaces"
        ).acknowledge(
            state, remote["id"], run["item"]["claim_token"], remote["finish_digest"]
        )
        if state.get("gates", {}).get("owned_run_id") == remote["id"]:
            GateEvaluator(self.journal).complete_run(state, remote["id"])
        for field in (
            "run",
            "context",
            "saved_plan",
            "delivery",
            "delivery_ready",
            "failure",
            "cancellation",
            "stage",
            "wait_reason",
            "request",
        ):
            state.pop(field, None)
        state["remote_active_run_id"] = None
        self.journal.save(state)
        return result

    def submit(self, report):
        entry = self.authorization()
        payload = {
            **report,
            "mode": self.mode,
            "environment_digest": entry["environment_digest"],
            "code_revision": entry["code_revision"],
        }
        return self.client.request("POST", self.path, {"report": payload})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--qualification")
    parser.add_argument("--item-id", type=int)
    parser.add_argument("--boundary", action="store_true")
    parser.add_argument("--workflow-run-url", default="")
    parser.add_argument("--boundary-evidence", type=Path)
    parser.add_argument("--record-result", action="store_true")
    parser.add_argument("--submit-evidence", action="store_true")
    parser.add_argument(
        "--submit",
        type=Path,
        help="Submit the protected live evidence bundle after all scenarios.",
    )
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(deployment_binding(), indent=2))
        return 0
    if not args.qualification:
        parser.error("Use --probe or --qualification UUID.")
    client = Client(
        os.environ.get("NQ_URL", "http://nightly-queue:8000"),
        os.environ.get("NQ_RUNNER_API_TOKEN", ""),
        max_bytes=8388608,
    )
    with Journal(
        Path(os.environ.get("NQ_RUNNER_STATE_DIR", "/var/lib/coding-runner"))
    ) as journal:
        state = journal.load()
        from .gates import GateEvaluator
        from .backend_registry import BackendRegistry, ScopedCredentials
        from .lifecycle import Lifecycle
        from .codex_backend import CodexBackend, CAPABILITIES, ProcessTransport
        import threading

        gates = GateEvaluator(journal)
        harness = ControlledRelease(client, journal, args.qualification)
        metadata_only = (
            args.boundary
            or args.record_result
            or args.submit_evidence
            or args.submit is not None
        )
        harness.recover(state, gates, execution=not metadata_only)
        if args.boundary:
            if not args.boundary_evidence:
                parser.error(
                    "--boundary requires the downloaded runner-boundary.json artifact via --boundary-evidence."
                )
            print(
                json.dumps(
                    harness.boundary(
                        state, args.workflow_run_url, args.boundary_evidence
                    )
                )
            )
            return 0
        if args.record_result:
            print(harness.record_result(state))
            return 0
        if args.submit_evidence:
            print(json.dumps(harness.submit(state.get("release_evidence", {}))))
            return 0
        if args.submit:
            print(json.dumps(harness.submit(json.loads(args.submit.read_text()))))
            return 0
        authorization = harness.authorization()
        if args.item_id and not state.get("run"):
            harness.claim(state, args.item_id)
        run_id = state.get("remote_active_run_id")
        if (state.get("run") or {}).get("run", {}).get("state") == "completed":
            parser.error(
                "The scenario is completed; use --record-result before another designated scenario."
            )
        if not run_id:
            parser.error(
                "Name --item-id for the designated queued scenario, or restore its existing journal."
            )

        def checkpoint():
            harness.authorization()
            remote = client.reconcile(run_id)
            if remote["run"]["state"] != "active":
                raise InvalidResponse("Controlled execution lost its active claim.")
            state["run"]["run"] = remote["run"]
            if remote["run"].get("cancellation_requested_at"):
                journal.save(state)
                return False
            configuration = client.configuration()
            # Only this explicitly authorized owned test lifecycle bypasses the missing release qualification.
            configuration["execution_ready"] = True
            gates.adapters.update(
                BackendRegistry().usage_adapters(
                    configuration["accounts"], ScopedCredentials(client)
                )
            )
            decision = gates.evaluate(
                configuration, state, checkpoint="stage", run_id=run_id
            )
            client.gate_report(decision.data())
            return decision.allowed

        configuration = client.configuration()
        versions = {
            entry["id"]: entry["secret_version"]
            for entry in configuration["credentials"]
        }

        def traced_backend(credentials):
            from .qualification_trace import ModelTraceTransport

            class ControlledCodex(CodexBackend):
                def readiness(self, account):
                    def probe():
                        if not checkpoint():
                            return None
                        response = ProcessTransport().exchange(
                            {
                                "probe_model": account["model"],
                                "token": self._token(account),
                                "timeout": 10,
                            },
                            timeout=10,
                            checkpoint=checkpoint,
                            stop=threading.Event(),
                        )
                        return response.get("authenticated_model")

                    return harness.candidate_readiness(
                        state, account, CAPABILITIES, probe
                    )

                def _run(self, stage, request, progress, checkpoint, stop):
                    self.transport = ModelTraceTransport(
                        journal,
                        state,
                        request.account,
                        versions[request.account["credential_id"]],
                    )
                    return super()._run(stage, request, progress, checkpoint, stop)

            return ControlledCodex(credentials)

        from .delivery import DeliveryStage, GitHubDeliveryClient, SupervisorGit

        def repository_client():
            run = state["run"]
            grant = client.request(
                "POST",
                "/api/v1/runner/secrets/",
                {
                    "scope": "run",
                    "run_id": run_id,
                    "claim_token": run["item"]["claim_token"],
                    "purpose": "repository",
                },
            )
            return grant["material"]["secret"]

        class RestartDelivery(DeliveryStage):
            def report(self, state, run_id, claim_token):
                if harness.observe_pr(state, self.github) < 2:
                    return {
                        "status": "restart_required",
                        "pull_request": state["delivery"]["pull_request"],
                    }
                return super().report(state, run_id, claim_token)

        def delivery_factory(durable_client, checkpoint):
            token = repository_client()
            return RestartDelivery(
                journal,
                durable_client,
                GitHubDeliveryClient(token),
                repository_git=SupervisorGit(token=token),
                gate=checkpoint,
            )

        lifecycle = Lifecycle(
            client,
            journal,
            BackendRegistry({"codex": traced_backend}),
            checkpoint,
            delivery_factory=delivery_factory,
        )
        if (state.get("delivery") or {}).get("phase") in {
            "delivered",
            "reporting_pending",
        }:
            harness.observe_pr(state, GitHubDeliveryClient(repository_client()))
        if authorization["repository_url"] != state["run"]["item"]["repository_url"]:
            raise InvalidResponse("The owned run is outside controlled scope.")
        print(harness.step(state, lifecycle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
