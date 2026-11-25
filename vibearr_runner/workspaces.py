from . import processes
import fcntl
import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID


class WorkspaceError(Exception):
    pass


def clean_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def git(arguments: list[str], cwd: Path | None = None, local: bool = False) -> str:
    command = [
        "/usr/bin/git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "protocol.file.allow=" + ("always" if local else "never"),
        "-c",
        "core.sshCommand=/usr/bin/false",
        "-c",
        "credential.helper=",
        "-c",
        "core.fsmonitor=false",
    ]
    result = processes.run(
        command + arguments,
        cwd=cwd,
        env=clean_environment(),
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        raise WorkspaceError(
            "Supervisor Git operation failed; repository content was not executed."
        )
    return result.stdout.decode().strip()


def validate_branch(branch: str) -> None:
    if not isinstance(branch, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch
    ):
        raise WorkspaceError("Invalid checkout branch.")
    git(["check-ref-format", "--branch", branch])


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if path.is_symlink() or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise WorkspaceError(
            "Workspace supervisor directories must be private and owned by the controller."
        )
    return path.resolve()


def content_digest(checkout: Path) -> str:
    result = hashlib.sha256()
    for path in sorted(checkout.rglob("*")):
        relative = path.relative_to(checkout)
        if relative.parts[0] == ".git":
            continue
        details = path.lstat()
        name = relative.as_posix().encode()
        result.update(len(name).to_bytes(8, "big") + name)
        result.update(stat.S_IMODE(details.st_mode).to_bytes(4, "big"))
        if stat.S_ISLNK(details.st_mode):
            result.update(b"link\0" + os.readlink(path).encode() + b"\0")
        elif stat.S_ISREG(details.st_mode):
            if details.st_nlink != 1:
                raise WorkspaceError("Hard-linked workspace files are forbidden.")
            result.update(b"file\0" + details.st_size.to_bytes(8, "big"))
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                while chunk := stream.read(65536):
                    result.update(chunk)
        elif stat.S_ISDIR(details.st_mode):
            result.update(b"directory\0")
        else:
            raise WorkspaceError("Special workspace files are forbidden.")
    return result.hexdigest()


@dataclass(frozen=True)
class Workspace:
    workspace_id: str
    repository_url: str
    requested_base_branch: str
    resolved_base_branch: str
    base_revision: str
    branch_name: str
    directory: Path

    @property
    def checkout(self) -> Path:
        return self.directory / "checkout"

    @contextmanager
    def locked(self):
        descriptor = os.open(
            self.directory / "workspace.lock",
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield self
        except BlockingIOError:
            raise WorkspaceError("Another operation owns this workspace.") from None
        finally:
            os.close(descriptor)

    def verify(self) -> str:
        if self.checkout.is_symlink() or (self.checkout / ".git").is_symlink():
            raise WorkspaceError("Workspace checkout metadata changed.")
        metadata = json.loads((self.directory / "workspace.json").read_text())
        expected = {**asdict(self), "directory": str(self.directory)}
        if metadata != expected:
            raise WorkspaceError("Workspace binding changed.")
        revision = git(
            [
                "rev-parse",
                "--verify",
                "refs/remotes/origin/" + self.resolved_base_branch + "^{commit}",
            ],
            self.checkout,
        )
        if revision != self.base_revision:
            raise WorkspaceError("Workspace base revision changed.")
        return content_digest(self.checkout)


class WorkspaceManager:
    def __init__(
        self,
        directory: Path,
        *,
        trusted_local_root: Path | None = None,
        remote_git=None,
        require_new_branch=False,
    ):
        self.directory = private_directory(directory)
        self.remote_git = remote_git
        self.require_new_branch = require_new_branch
        self.resume_clone = False
        self.trusted_local_root = (
            trusted_local_root.resolve() if trusted_local_root else None
        )

    def prepare(self, run_id: str, item: dict) -> Workspace:
        workspace_id = str(UUID(run_id))
        repository = item["repository_url"]
        base = item["base_branch"]
        branch = item["branch_name"]
        validate_branch(branch)
        if base:
            validate_branch(base)
            if branch == base:
                raise WorkspaceError("Task branch must differ from the base branch.")
        local = False
        parsed = urlsplit(repository)
        if self.trusted_local_root and not parsed.scheme:
            source = Path(repository).resolve()
            local = source.is_relative_to(self.trusted_local_root) and source.is_dir()
        invalid = parsed.username or parsed.password or parsed.query or parsed.fragment
        remote = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and bool(parsed.path)
            and not invalid
        )
        if not local and not remote:
            raise WorkspaceError(
                "Use an HTTPS repository URL without credentials or options."
            )
        directory = private_directory(self.directory / workspace_id)
        with Workspace(
            workspace_id, repository, base, base, "", branch, directory
        ).locked():
            metadata = directory / "workspace.json"
            if metadata.exists():
                saved = json.loads(metadata.read_text())
                workspace = Workspace(**{**saved, "directory": directory})
                expected = (repository, base, branch)
                actual = (
                    workspace.repository_url,
                    workspace.requested_base_branch,
                    workspace.branch_name,
                )
                if actual != expected or saved["workspace_id"] != workspace_id:
                    raise WorkspaceError(
                        "A previous checkout has different claim settings."
                    )
                if branch == workspace.resolved_base_branch:
                    raise WorkspaceError(
                        "Task branch must differ from the base branch."
                    )
                workspace.verify()
                return workspace
            checkout = directory / "checkout"
            if checkout.exists() and not self.resume_clone:
                raise WorkspaceError(
                    "An incomplete checkout requires operator recovery."
                )
            arguments = [
                "clone",
                "--no-local",
                "--no-checkout",
                "--no-recurse-submodules",
                "--",
                repository,
                str(checkout),
            ]
            if not checkout.exists():
                if self.remote_git is not None and not local:
                    self.remote_git(arguments)
                else:
                    git(arguments, local=local)
            resolved = base
            if not resolved:
                symbolic = git(["symbolic-ref", "refs/remotes/origin/HEAD"], checkout)
                prefix = "refs/remotes/origin/"
                if not symbolic.startswith(prefix):
                    raise WorkspaceError("Remote default branch could not be resolved.")
                resolved = symbolic[len(prefix) :]
            validate_branch(resolved)
            if branch == resolved:
                raise WorkspaceError("Task branch must differ from the base branch.")
            revision = git(
                [
                    "rev-parse",
                    "--verify",
                    "refs/remotes/origin/" + resolved + "^{commit}",
                ],
                checkout,
            )
            existing = (
                processes.run(
                    [
                        "/usr/bin/git",
                        "show-ref",
                        "--verify",
                        "--quiet",
                        "refs/remotes/origin/" + branch,
                    ],
                    cwd=checkout,
                    env=clean_environment(),
                    capture_output=True,
                    timeout=15,
                ).returncode
                == 0
            )
            if existing and self.require_new_branch:
                raise WorkspaceError(
                    "An existing remote task branch requires publication reconciliation."
                )
            start = "refs/remotes/origin/" + (branch if existing else resolved)
            current = git(["symbolic-ref", "--short", "HEAD"], checkout)
            if current == branch:
                git(["checkout", branch, "--"], checkout)
            else:
                git(["checkout", "--no-track", "-b", branch, start, "--"], checkout)
            workspace = Workspace(
                workspace_id, repository, base, resolved, revision, branch, directory
            )
            metadata.write_text(
                json.dumps(
                    {**asdict(workspace), "directory": str(directory)}, sort_keys=True
                )
            )
            metadata.chmod(0o600)
            workspace.verify()
            return workspace

    def inspect_base(self, workspace: Workspace):
        arguments = [
            "ls-remote",
            "--refs",
            "--",
            workspace.repository_url,
            "refs/heads/" + workspace.resolved_base_branch,
        ]
        if self.remote_git:
            observed = self.remote_git(arguments, cwd=workspace.checkout)
        else:
            observed = git(arguments, local=self.trusted_local_root is not None)
        expected = (
            workspace.base_revision + "\trefs/heads/" + workspace.resolved_base_branch
        )
        if observed != expected:
            raise WorkspaceError(
                "Current remote base changed; preserve work and save an assessed replacement plan before resuming."
            )
