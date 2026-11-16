"""Verified file import and recoverable commits in the trusted checkout."""

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import UUID

from .workspaces import Workspace, WorkspaceError, git, private_directory


class NoChange(WorkspaceError):
    pass


def atomic_bytes(path: Path, content: bytes):
    temporary = path.with_suffix(path.suffix + ".pending")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def artifact_file(workspace: Workspace, operation_id: str, name: str) -> Path:
    operation = str(UUID(operation_id))
    directory = private_directory(workspace.directory / "native" / operation)
    if name not in {
        "input.tar",
        "output.tar",
        "result.json",
        "events.json",
        "logs.bin",
    }:
        raise WorkspaceError("Unknown native artifact name.")
    return directory / name


def save_artifact(workspace, operation_id, name, content):
    path = artifact_file(workspace, operation_id, name)
    if path.exists() and path.read_bytes() != content:
        raise WorkspaceError("An immutable native artifact changed.")
    if not path.exists():
        atomic_bytes(path, content)
    return hashlib.sha256(content).hexdigest()


def import_files(workspace, source, result, inventory, operation_id, journal, state):
    from vibearr_executor.artifacts import extract, result_manifest, snapshot
    from vibearr_executor.contracts import canonical

    if canonical(result_manifest(source, result)) != canonical(inventory):
        raise WorkspaceError("Native export differs from its verified file inventory.")
    save_artifact(workspace, operation_id, "input.tar", source)
    save_artifact(workspace, operation_id, "output.tar", result)
    intent = {
        "operation_id": str(UUID(operation_id)),
        "source_digest": hashlib.sha256(source).hexdigest(),
        "result_digest": hashlib.sha256(result).hexdigest(),
    }
    previous = state["native"].get("import")
    if previous and previous != intent:
        raise WorkspaceError("Another native import still requires reconciliation.")
    state["native"]["import"] = intent
    journal.save(state)
    root = private_directory(workspace.directory / "imports" / intent["operation_id"])
    target = root / "checkout"
    marker = root / "ready.json"
    if not marker.exists():
        if snapshot(workspace.checkout) != source:
            raise WorkspaceError("Trusted checkout changed before native import.")
        if target.exists():
            shutil.rmtree(target)
        extract(result, target)
        shutil.copytree(workspace.checkout / ".git", target / ".git")
        if snapshot(target) != result:
            raise WorkspaceError("Prepared native import did not match its artifact.")
        atomic_bytes(marker, canonical(intent))
    recover_import(workspace.directory, journal, state)


def recover_import(directory: Path, journal, state):
    from vibearr_executor.artifacts import snapshot
    from vibearr_executor.contracts import canonical

    intent = state.get("native", {}).get("import")
    if not intent:
        return
    operation = str(UUID(intent["operation_id"]))
    root = private_directory(directory / "imports" / operation)
    target, old, checkout = root / "checkout", root / "previous", directory / "checkout"
    marker = root / "ready.json"
    if not marker.exists():
        return
    if marker.read_bytes() != canonical(intent):
        raise WorkspaceError("Native import recovery binding changed.")
    expected = intent["result_digest"]
    if checkout.exists() and hashlib.sha256(snapshot(checkout)).hexdigest() == expected:
        state["native"].pop("import", None)
        journal.save(state)
        return
    if not target.is_dir() or hashlib.sha256(snapshot(target)).hexdigest() != expected:
        raise WorkspaceError("Prepared native import is unavailable or changed.")
    if checkout.exists():
        if (
            old.exists()
            or hashlib.sha256(snapshot(checkout)).hexdigest() != intent["source_digest"]
        ):
            raise WorkspaceError(
                "Unexpected trusted checkout during native import recovery."
            )
        os.rename(checkout, old)
    elif not old.is_dir():
        raise WorkspaceError("Original native checkout is unavailable.")
    os.rename(target, checkout)
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    state["native"].pop("import", None)
    journal.save(state)


def commit_candidate(workspace, *, base_tree, operation_id, title, journal, state):
    native = state["native"]
    record = native.get("commit")
    if record and record["operation_id"] != operation_id:
        raise WorkspaceError("Previous candidate commit requires reconciliation.")
    head = git(["rev-parse", "HEAD"], workspace.checkout)
    if record is None:
        record = {"operation_id": operation_id, "parent": head, "phase": "staging"}
        native["commit"] = record
        journal.save(state)
    message = f"{title[:72]}\n\nVibearr-Run: {workspace.workspace_id}\nVibearr-Operation: {operation_id}"
    if head != record["parent"]:
        valid = record.get("tree") == git(
            ["rev-parse", "HEAD^{tree}"], workspace.checkout
        )
        valid = (
            valid
            and git(["rev-parse", "HEAD^"], workspace.checkout) == record["parent"]
        )
        valid = (
            valid and git(["log", "-1", "--format=%B"], workspace.checkout) == message
        )
        if not valid:
            raise WorkspaceError("Candidate commit identity requires reconciliation.")
    else:
        if any(
            value.get("status") != "stopped"
            for value in state.get("processes", {}).values()
        ):
            raise WorkspaceError("A trusted Git process still owns the checkout.")
        for name in ("index.lock", f"refs/heads/{workspace.branch_name}.lock"):
            lock = workspace.checkout / ".git" / name
            if lock.is_symlink():
                raise WorkspaceError("Linked Git lock file is forbidden.")
            lock.unlink(missing_ok=True)
        git(
            ["-c", "core.autocrlf=false", "add", "--all", "--", "."], workspace.checkout
        )
        tree = git(["write-tree"], workspace.checkout)
        if tree == base_tree:
            raise NoChange("The native task produced no changed committed tree.")
        record.update(tree=tree, phase="commit_pending")
        journal.save(state)
        if tree != git(["rev-parse", "HEAD^{tree}"], workspace.checkout):
            git(
                [
                    "-c",
                    "user.name=vibearr",
                    "-c",
                    "user.email=runner@localhost",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "--no-verify",
                    "--message",
                    message,
                ],
                workspace.checkout,
            )
        head = git(["rev-parse", "HEAD"], workspace.checkout)
    git(["clean", "-ffdx", "--", "."], workspace.checkout)
    record.update(commit_sha=head, phase="committed")
    journal.save(state)
    return head


def update_repository(workspace, repository_url):
    if workspace.repository_url == repository_url:
        return workspace
    from .context import repository_path

    repository_path(repository_url)
    updated = replace(workspace, repository_url=repository_url)
    git(["remote", "set-url", "origin", repository_url], workspace.checkout)
    metadata = {**asdict(updated), "directory": str(updated.directory)}
    atomic_bytes(
        updated.directory / "workspace.json",
        json.dumps(metadata, sort_keys=True).encode(),
    )
    return updated
