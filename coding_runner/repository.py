import base64
import re

from .process import TaskError


class Repository:
    def __init__(self, commands, workspace, env, name, token):
        self.commands = commands
        self.workspace = workspace
        self.env = env
        self.name = name
        self.url = f"https://github.com/{name}.git"
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        self.remote_env = dict(env)
        self.remote_env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
            }
        )
        commands.secrets.append(encoded)

    def git(self, *args, remote=False):
        env = self.remote_env if remote else self.env
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "http.followRedirects=false",
            *args,
        ]
        return self.commands.run(command, self.workspace, env)

    def checkout(self, item):
        branch = item["branch_name"]
        if branch != f"nq/{item['id']}":
            raise TaskError("Unexpected task branch name.")
        self.workspace.mkdir()
        self.git("clone", "--no-checkout", "--", self.url, ".", remote=True)
        base = item["base_branch"]
        if not base:
            default = self.git("symbolic-ref", "refs/remotes/origin/HEAD")
            base = default.removeprefix("refs/remotes/origin/")
        self.git("check-ref-format", "--branch", base)
        if base == branch:
            raise TaskError("The task branch cannot be the base branch.")
        self.git("rev-parse", "--verify", f"refs/remotes/origin/{base}")
        refs = self.git(
            "for-each-ref", "--format=%(refname)", f"refs/remotes/origin/{branch}"
        )
        start = branch if refs else base
        self.git("checkout", "-b", branch, f"refs/remotes/origin/{start}")
        self.original_config = (self.workspace / ".git" / "config").read_bytes()
        return base

    def deliver(self, branch, base):
        config_path = self.workspace / ".git" / "config"
        if config_path.is_symlink() or config_path.read_bytes() != self.original_config:
            raise TaskError(
                "The task changed Git configuration; review it before delivery."
            )
        current = self.git("branch", "--show-current")
        if current != branch:
            raise TaskError("The agent changed the task branch.")
        self.git("add", "--all")
        changed = self.git("diff", "--cached", "--name-only")
        if changed:
            self.git("commit", "-m", f"Implement queue task {branch}")
        diff = self.git("diff", "--name-only", f"refs/remotes/origin/{base}...HEAD")
        if not diff:
            raise TaskError("No changes to deliver; review the task manually.")
        commit = self.git("rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise TaskError("Invalid task commit.")
        self.git("push", self.url, f"HEAD:refs/heads/{branch}", remote=True)
        remote = self.git("ls-remote", self.url, f"refs/heads/{branch}", remote=True)
        if remote.split()[:1] != [commit]:
            raise TaskError("Unable to verify the pushed task commit.")
        return commit
