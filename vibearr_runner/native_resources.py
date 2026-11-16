"""Private native resource references survive workspace and log retention separately."""

import json
from pathlib import Path
from uuid import UUID

from .native_workspace import atomic_bytes
from .workspaces import WorkspaceError, private_directory
from .recovery import ReconciliationRequired


class NativeResources:
    def __init__(self, directory: Path):
        self.directory = private_directory(directory / "native-resources")

    def path(self, run_id):
        return self.directory / (str(UUID(run_id)) + ".json")

    def load(self, run_id):
        path = self.path(run_id)
        if path.is_symlink():
            raise WorkspaceError("Linked native resource record is forbidden.")
        if not path.exists():
            return {
                "schema_version": 1,
                "run_id": run_id,
                "operations": {},
                "builds": {},
                "removed": [],
            }
        value = json.loads(path.read_bytes())
        if value.get("schema_version") != 1 or value.get("run_id") != run_id:
            raise WorkspaceError("Native resource record belongs to another run.")
        return value

    def save(self, value):
        content = json.dumps(value, sort_keys=True, allow_nan=False).encode()
        if len(content) > 1024 * 1024:
            raise WorkspaceError("Native resource references exceed their limit.")
        atomic_bytes(self.path(value["run_id"]), content)

    def bind(self, run_id, executor):
        value = self.load(run_id)
        identity = executor.capabilities().get("instance_id")
        try:
            if not isinstance(identity, str) or str(UUID(identity)) != identity:
                raise ValueError
        except ValueError:
            raise ReconciliationRequired(
                "Executor has no stable execution-state identity."
            ) from None
        if value.get("instance_id") not in {None, identity}:
            raise ReconciliationRequired(
                "Executor state was replaced; original native operations require reconciliation."
            )
        if not value.get("instance_id"):
            if value["operations"] or value["builds"]:
                raise ReconciliationRequired(
                    "Existing native resources have no executor-state proof."
                )
            value["instance_id"] = identity
            self.save(value)
        return identity

    def operation(self, run_id, manifest, *, kind):
        if manifest["run_id"] != run_id:
            raise WorkspaceError("Native operation belongs to another run.")
        operation_id = str(UUID(manifest["operation_id"]))
        value = self.load(run_id)
        record = {"kind": kind, "manifest": manifest}
        previous = value["operations"].get(operation_id)
        if previous is not None and previous != record:
            raise WorkspaceError("Native operation reference changed.")
        value["operations"][operation_id] = record
        value.setdefault("pending", {}).pop(operation_id, None)
        self.save(value)

    def reserve(self, run_id, operation_id, *, build=False):
        value = self.load(run_id)
        operation_id = str(UUID(operation_id))
        key = "pending_builds" if build else "pending"
        known = "builds" if build else "operations"
        if operation_id in value[known]:
            return
        value.setdefault(key, {})[operation_id] = True
        if build:
            value.setdefault("pending", {})[operation_id] = True
        self.save(value)

    def build(self, run_id, request):
        if request["run_id"] != run_id:
            raise WorkspaceError("Dependency build belongs to another run.")
        build_id = str(UUID(request["build_id"]))
        value = self.load(run_id)
        previous = value["builds"].get(build_id)
        if previous is not None and previous != request:
            raise WorkspaceError("Native dependency build reference changed.")
        value["builds"][build_id] = request
        value.setdefault("pending_builds", {}).pop(build_id, None)
        value.setdefault("pending", {}).pop(build_id, None)
        self.save(value)

    def dispatch(self, run_id, identity, *, build=False):
        value = self.load(run_id)
        if identity not in value["builds" if build else "operations"]:
            raise ReconciliationRequired(
                "Register the immutable native resource before dispatch."
            )
        key = "dispatched_builds" if build else "dispatched"
        value.setdefault(key, {})[identity] = True
        self.save(value)

    def verify_dispatched(self, run_id, executor):
        from vibearr_executor.client import ExecutorRejected

        value = self.load(run_id)
        self.bind(run_id, executor)
        for identity in value.get("dispatched", {}):
            observed = executor.status(identity)
            if observed.get("state") == "not-started":
                raise ReconciliationRequired(
                    "A dispatched native operation is missing or unconfirmed; never recreate its record."
                )
        for identity in value.get("dispatched_builds", {}):
            try:
                executor.dependencies(identity, "status")
            except ExecutorRejected as exc:
                if exc.http_status == 404:
                    raise ReconciliationRequired(
                        "A dispatched dependency build record is missing; retain ownership."
                    ) from None
                raise

    def reconcile(self, run_id, executor, *, stop=False):
        self.bind(run_id, executor)
        self.verify_dispatched(run_id, executor)
        value = self.load(run_id)
        operations = []
        builds = []
        for operation_id, entry in value["operations"].items():
            action = executor.stop if stop else executor.status
            operations.append(action(operation_id))
        for build_id in value["builds"]:
            builds.append(executor.dependencies(build_id, "stop" if stop else "status"))
            operations.append(executor.status(build_id))
        if stop:
            self.stop_pending(value, executor)
        return {"operations": operations, "builds": builds}

    def stop_pending(self, value, executor):
        from vibearr_executor.client import ExecutorRejected

        for key in ("pending", "pending_builds"):
            for identity in list(value.get(key, {})):
                try:
                    observed = (
                        executor.stop(identity)
                        if key == "pending"
                        else executor.dependencies(identity, "stop")
                    )
                except ExecutorRejected as exc:
                    if exc.http_status != 404:
                        raise
                    value[key].pop(identity)
                    self.save(value)
                    continue
                terminated = (
                    observed.get("descendants_terminated")
                    if key == "pending"
                    else observed.get("resources_terminated")
                )
                if terminated is not True:
                    raise ReconciliationRequired(
                        "Prepared native resource termination is uncertain."
                    )

    def controls(self, run_id, executor, *, pause):
        self.bind(run_id, executor)
        value = self.load(run_id)
        observations = []
        for build_id in value["builds"]:
            method = (
                executor.pause_dependencies if pause else executor.resume_dependencies
            )
            observations.append(method(build_id))
        for operation_id, entry in value["operations"].items():
            if operation_id in value["builds"]:
                continue
            observed = executor.status(operation_id)
            if observed.get("state") != "running":
                continue
            method = executor.pause if pause else executor.resume
            observations.append(method(operation_id))
        return observations

    def purge(self, run_id, executor, resource):
        value = self.load(run_id)
        if resource in value["removed"]:
            return
        if not any(
            value.get(key)
            for key in ("operations", "builds", "pending", "pending_builds")
        ):
            return
        self.bind(run_id, executor)
        target = "source" if resource == "workspace" else "logs"
        for operation_id in set(value["operations"]) | set(value.get("pending", {})):
            if operation_id not in value["builds"]:
                executor.purge(operation_id, run_id=run_id, resources=[target])
        for build_id in set(value["builds"]) | set(value.get("pending_builds", {})):
            executor.purge_dependencies(build_id, run_id=run_id, resources=[target])
        value["removed"].append(resource)
        self.save(value)
        if set(value["removed"]) == {"workspace", "logs"}:
            self.path(run_id).unlink()
