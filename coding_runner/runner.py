import re
import sys
import uuid

from .codex import Codex
from .config import repository_name
from .http import APIError, GitHubClient, QueueClient
from .logs import WebLogs
from .process import Commands, TaskError, clean_environment
from .repository import Repository
from .state import Journal


class Runner:
    def __init__(self, config, queue=None, github=None, agent=None):
        self.config = config
        self.queue = queue or QueueClient(config.url, config.token)
        self.github = github or GitHubClient(config.github_token)
        self.agent = agent or Codex(config.openai_key, config.model)
        self.journal = Journal(config.data_dir)

    def preflight(self):
        self.queue.check()
        self.queue.lookup(str(uuid.uuid4()))
        self.github.check()
        commands = Commands(self.config.data_dir / "preflight.log", 30)
        env = clean_environment(self.config.data_dir)
        commands.run(["git", "--version"], self.config.data_dir, env)
        commands.run(["codex", "--version"], self.config.data_dir, env)
        sandbox = [
            "codex",
            "sandbox",
            "-c",
            'sandbox_mode="workspace-write"',
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
        commands.run(sandbox, self.config.data_dir, env)

    def recover(self, discard=False):
        state = self.journal.read()
        if state is None:
            return
        item = self.queue.lookup(state["run_id"])
        if discard:
            if item and item["status"] == "running":
                raise RuntimeError(
                    "Stop and requeue this execution in admin before discarding its journal."
                )
            state["phase"] = "discarded"
            self.journal.archive(state)
            return
        outcome = state.get("outcome")
        if state["phase"] in ("finishing", "finished") and outcome and item:
            if item["status"] == outcome["status"] and item.get(
                "result_url", ""
            ) == outcome.get("result_url", ""):
                self.complete(state)
                return
            original = state["item"]
            if (
                item["status"] == "running"
                and item.get("claim_token") == original["claim_token"]
            ):
                self.queue.finish(original, outcome)
                self.complete(state)
                return
        raise RuntimeError(
            "An interrupted or ambiguous run needs review. Inspect active.json and queue admin; "
            "stop the old execution, requeue it if needed, then use --discard-interrupted."
        )

    def complete(self, state):
        state["phase"] = "finished"
        self.journal.write(state)
        self.journal.archive(state)

    def once(self):
        if self.journal.read() is not None:
            raise RuntimeError("Resolve the previous run before claiming another item.")
        if not self.config.window_open():
            return False
        run_id = str(uuid.uuid4())
        state = {"run_id": run_id, "phase": "claiming"}
        self.journal.write(state)
        item = self.queue.claim(run_id)
        if item is None:
            state["phase"] = "empty"
            self.journal.archive(state)
            return False
        state.update(item=item, phase="claimed")
        self.journal.write(state)
        self.web_logs = WebLogs(self.queue, item, run_id)
        self.web_logs.emit("Task claimed.\n")
        try:
            outcome = self.execute(state)
        except APIError:
            self.web_logs.emit(
                "\nDelivery could not be confirmed. Runner stopped for review.\n"
            )
            raise
        except (TaskError, ValueError, OSError) as exc:
            summary = str(exc)
            for secret in (
                self.config.token,
                self.config.github_token,
                self.config.openai_key,
                item["claim_token"],
            ):
                summary = summary.replace(secret, "[REDACTED]") if secret else summary
            outcome = {"status": "failed", "error_summary": summary[:2000]}
            self.web_logs.emit(f"Task failed: {summary[:2000]}\n")
        finally:
            self.web_logs.close()
        state.update(phase="finishing", outcome=outcome)
        self.journal.write(state)
        self.queue.finish(item, outcome)
        self.complete(state)
        print(f"Task {item['id']}: {outcome['status']}", flush=True)
        return True

    def execute(self, state):
        item = state["item"]
        name = repository_name(item["repository_url"])
        policy = self.config.policies.get(name)
        if policy is None:
            raise TaskError("Repository has no operator-approved runner policy.")
        run_dir = self.journal.run_dir(state["run_id"])
        home = run_dir / "home"
        home.mkdir()
        workspace = run_dir / "repository"
        env = clean_environment(home)
        secrets = (
            self.config.token,
            self.config.github_token,
            self.config.openai_key,
            item["claim_token"],
        )
        commands = Commands(
            run_dir / "commands.log",
            self.config.timeout_seconds,
            secrets,
            on_output=self.web_logs.emit,
        )
        repository = Repository(
            commands, workspace, env, name, self.config.github_token
        )
        self.phase(state, "checkout")
        base = repository.checkout(item)
        state["resolved_base"] = base
        self.phase(state, "setup")
        for command in policy.get("setup", []):
            commands.run(command, workspace, env)
        self.phase(state, "agent")
        self.agent.run(commands, workspace, env, item)
        self.phase(state, "validation")
        for command in policy["validation"]:
            commands.run(command, workspace, env)
        self.phase(state, "delivery")
        state["commit"] = repository.deliver(item["branch_name"], base)
        self.journal.write(state)
        body = f"Implements queue item #{item['id']}.\n\nValidation completed:\n"
        body += "\n".join(
            f"- `{command[0]}` (exit 0)" for command in policy["validation"]
        )
        if item.get("source") == "github" and type(item.get("issue_number")) is int:
            body += f"\n\nCloses #{item['issue_number']}."
        # Ambiguous delivery errors retain the journal instead of replaying the agent.
        url = self.github.pull_request(
            name, item["branch_name"], base, item["title"], body
        )
        if not isinstance(url, str) or not re.fullmatch(
            rf"https://github.com/{re.escape(name)}/pull/[1-9][0-9]*", url, re.I
        ):
            raise APIError(
                "GitHub returned an invalid PR URL; inspect delivery before retrying."
            )
        self.web_logs.emit(f"PR delivered: {url}\n")
        return {"status": "succeeded", "result_url": url}

    def phase(self, state, phase):
        state["phase"] = phase
        self.journal.write(state)
        self.web_logs.emit(f"\n[{phase}]\n")
