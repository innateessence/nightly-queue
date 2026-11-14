import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


def repository_name(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        raise ValueError("Only HTTPS github.com repositories are supported.")
    if parsed.query or parsed.fragment:
        raise ValueError("Repository URLs cannot contain a query or fragment.")
    name = parsed.path.removesuffix(".git").strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", name):
        raise ValueError("Invalid GitHub repository path.")
    if any(part in (".", "..") for part in name.split("/")):
        raise ValueError("Invalid GitHub repository path.")
    return name.lower()


def command_lists(value):
    if not isinstance(value, list):
        raise ValueError("Commands must be JSON arrays of argument arrays.")
    for command in value:
        if not isinstance(command, list) or not command:
            raise ValueError("Each command must be a nonempty argument array.")
        if any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command):
            raise ValueError("Command arguments must be nonempty strings.")
    return value


@dataclass
class Config:
    url: str
    token: str
    github_token: str
    openai_key: str
    data_dir: Path
    policies: dict
    model: str = ""
    poll_seconds: int = 60
    timeout_seconds: int = 3600
    timezone: str = "UTC"
    window_start: str = ""
    window_end: str = ""

    @classmethod
    def from_env(cls):
        url = os.environ.get("NQ_URL", "http://nightly-queue:8000").rstrip("/")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("NQ_URL must be an HTTP(S) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "NQ_URL cannot contain credentials, a query, or a fragment."
            )
        required = ["NQ_API_TOKEN", "GH_TOKEN", "OPENAI_API_KEY", "NQ_RUNNER_PROJECTS"]
        for name in required:
            if not os.environ.get(name):
                raise ValueError(f"Set {name} before starting the runner.")
        policies = json.loads(Path(os.environ["NQ_RUNNER_PROJECTS"]).read_text())
        if not isinstance(policies, dict) or not policies:
            raise ValueError("Configure at least one repository policy.")
        for name, policy in policies.items():
            if repository_name(f"https://github.com/{name}") != name:
                raise ValueError(
                    "Policy keys must be lowercase owner/repository names."
                )
            if not isinstance(policy, dict):
                raise ValueError("Each repository policy must be an object.")
            if not command_lists(policy.get("validation", [])):
                raise ValueError("Every repository needs a validation command.")
            command_lists(policy.get("setup", []))
        config = cls(
            url=url,
            token=os.environ["NQ_API_TOKEN"],
            github_token=os.environ["GH_TOKEN"],
            openai_key=os.environ["OPENAI_API_KEY"],
            data_dir=Path(os.environ.get("NQ_RUNNER_DATA", "/data")).resolve(),
            policies=policies,
            model=os.environ.get("NQ_RUNNER_MODEL", ""),
            poll_seconds=int(os.environ.get("NQ_RUNNER_POLL_SECONDS", "60")),
            timeout_seconds=int(os.environ.get("NQ_RUNNER_TIMEOUT_SECONDS", "3600")),
            timezone=os.environ.get("NQ_RUNNER_TIMEZONE", "UTC"),
            window_start=os.environ.get("NQ_RUNNER_WINDOW_START", ""),
            window_end=os.environ.get("NQ_RUNNER_WINDOW_END", ""),
        )
        if config.poll_seconds < 1 or config.timeout_seconds < 1:
            raise ValueError("Polling interval and task timeout must be positive.")
        ZoneInfo(config.timezone)
        if bool(config.window_start) != bool(config.window_end):
            raise ValueError("Set both execution window boundaries or neither.")
        if config.window_start:
            start = time.fromisoformat(config.window_start)
            end = time.fromisoformat(config.window_end)
            if start == end:
                raise ValueError("Execution window boundaries must differ.")
        return config

    def window_open(self, now=None):
        if not self.window_start:
            return True
        now = now or datetime.now(ZoneInfo(self.timezone))
        local_time = now.astimezone(ZoneInfo(self.timezone)).time()
        start = time.fromisoformat(self.window_start)
        end = time.fromisoformat(self.window_end)
        if start < end:
            return start <= local_time < end
        return local_time >= start or local_time < end
