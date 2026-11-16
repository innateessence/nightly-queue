"""A fixed observer reads the counter without executing in the frozen task."""

import json
from pathlib import Path

from .contracts import Conflict, canonical, digest
from .engine import Docker, EngineError

COMMAND = [
    "-I",
    "-c",
    "import os,signal,stat,sys;signal.alarm(5);"
    "fd=os.open('/proc/1/root/work/pause-count.txt',os.O_RDONLY|os.O_NOFOLLOW);"
    "info=os.fstat(fd);"
    "assert stat.S_ISREG(info.st_mode) and info.st_size<=32;"
    "value=os.read(fd,33);assert value.isdigit() and len(value)<=32;"
    "sys.stdout.buffer.write(value)",
]
CAPABILITIES = {"SYS_PTRACE", "DAC_READ_SEARCH"}
PURPOSE = "qualification-counter-v1"


def save(path: Path, value: dict) -> None:
    from .runtime import durable

    durable(path, canonical(value))


def owned(docker: Docker, intent: dict) -> dict | None:
    current = docker.inspect(intent["name"])
    if current is None:
        return None
    config, host = current["Config"], current["HostConfig"]
    labels = config.get("Labels", {})
    expected = {
        "vibearr.executor": intent["instance_id"],
        "vibearr.operation": intent["operation_id"],
        "vibearr.purpose": PURPOSE,
        "vibearr.counter-recipe": intent["recipe_digest"],
    }
    caps = {value.removeprefix("CAP_") for value in host.get("CapAdd", [])}
    changed = intent.get("container_id") and current["Id"] != intent["container_id"]
    safe = (
        not changed
        and all(labels.get(key) == value for key, value in expected.items())
        and current["Image"] == intent["image"]
        and config["Entrypoint"] == ["/usr/local/bin/python3.12"]
        and config["Cmd"] == intent["command"]
        and config["User"] == "0:0"
        and host["PidMode"] == "container:" + intent["target_container_id"]
        and host["NetworkMode"] == "none"
        and host["ReadonlyRootfs"] is True
        and caps == CAPABILITIES
        and host.get("CapDrop") == ["ALL"]
        and host["Memory"] == 64 * 1024**2
        and host["MemorySwap"] == 64 * 1024**2
        and host["PidsLimit"] == 16
        and host["NanoCpus"] == 500000000
        and "no-new-privileges:true" in host.get("SecurityOpt", [])
        and not current.get("Mounts")
    )
    if not safe:
        raise Conflict("Qualification observer identity changed; retain its fence.")
    return current


def cleanup(docker: Docker, directory: Path, record: dict, instance: str) -> None:
    path = directory / "counter-reader.json"
    if not path.exists():
        return
    intent = json.loads(path.read_bytes())
    valid = (
        intent.get("schema_version") == 1
        and intent.get("instance_id") == instance
        and intent.get("operation_id") == record["operation_id"]
        and intent.get("target_container_id") == record.get("container_id")
        and intent.get("image") == record["profile"]["image"]
        and intent.get("name") == record["name"] + "-counter"
        and isinstance(intent.get("command"), list)
        and intent.get("recipe_digest") == digest(canonical(intent["command"]))
    )
    if not valid:
        raise Conflict(
            "Qualification observer journal does not bind the original task."
        )
    current = owned(docker, intent)
    if current is not None:
        docker.remove("container", current["Id"])
    if owned(docker, intent) is not None:
        raise EngineError("Qualification observer removal was not confirmed.")
    intent["removed"] = True
    save(path, intent)


class CounterObserver:
    def __init__(self, state: Path, operation_id: str, docker: Docker):
        self.directory = state / "operations" / operation_id
        self.record = json.loads((self.directory / "record.json").read_bytes())
        self.instance = (state / "instance").read_text()
        self.docker = docker
        from .control_qualification import control_profile

        if (
            self.record["profile"]
            != control_profile(self.record["profile"]["image"]).data()
        ):
            raise Conflict(
                "Counter observation is restricted to the fixed qualification recipe."
            )

    def paused(self) -> dict:
        original = self.docker.inspect(self.record["container_id"])
        labels = original["Config"].get("Labels", {}) if original else {}
        if (
            original is None
            or original["Id"] != self.record["container_id"]
            or not original["State"].get("Running")
            or not original["State"].get("Paused")
            or labels.get("vibearr.executor") != self.instance
            or labels.get("vibearr.operation") != self.record["operation_id"]
        ):
            raise EngineError(
                "The exact original task must remain paused during counter observation."
            )
        return original

    def read(self) -> dict:
        original = self.paused()
        cleanup(self.docker, self.directory, self.record, self.instance)
        image = self.record["profile"]["image"]
        installed = json.loads(self.docker.run("image", "inspect", image))[0]
        if installed["Config"].get("Volumes"):
            raise EngineError(
                "Qualification counter observers cannot inherit image volumes."
            )
        intent = {
            "schema_version": 1,
            "instance_id": self.instance,
            "operation_id": self.record["operation_id"],
            "target_container_id": original["Id"],
            "name": self.record["name"] + "-counter",
            "image": image,
            "command": COMMAND,
            "recipe_digest": digest(canonical(COMMAND)),
            "removed": False,
        }
        path = self.directory / "counter-reader.json"
        save(path, intent)
        arguments = [
            "create",
            "--name",
            intent["name"],
            "--read-only",
            "--network",
            "none",
            "--user",
            "0:0",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SYS_PTRACE",
            "--cap-add",
            "DAC_READ_SEARCH",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "64m",
            "--memory-swap",
            "64m",
            "--pids-limit",
            "16",
            "--cpus",
            ".5",
            "--pid",
            "container:" + original["Id"],
        ]
        for key, value in {
            "vibearr.executor": self.instance,
            "vibearr.operation": self.record["operation_id"],
            "vibearr.purpose": PURPOSE,
            "vibearr.counter-recipe": intent["recipe_digest"],
        }.items():
            arguments.extend(("--label", key + "=" + value))
        arguments.extend(("--entrypoint", "/usr/local/bin/python3.12", image, *COMMAND))
        try:
            self.docker.run(*arguments, timeout=10)
            current = owned(self.docker, intent)
            if current is None:
                raise EngineError(
                    "Qualification observer create outcome is unavailable."
                )
            intent["container_id"] = current["Id"]
            save(path, intent)
            self.paused()
            output = self.docker.run("start", "-a", current["Id"], limit=32, timeout=10)
            completed = owned(self.docker, intent)
            if (
                completed is None
                or completed["State"]["Running"]
                or completed["State"]["ExitCode"] != 0
            ):
                raise EngineError(
                    "Qualification observer did not confirm a successful exit."
                )
            if not output.isdigit() or len(output) > 32:
                raise EngineError(
                    "Qualification counter output violates its fixed integer bound."
                )
            after = self.paused()
            if after["State"]["Pid"] != original["State"]["Pid"]:
                raise EngineError(
                    "The original paused task PID changed during observation."
                )
            return {
                "counter": int(output),
                "observer_container_id": current["Id"],
                "target_container_id": original["Id"],
                "target_pid": original["State"]["Pid"],
                "image": image,
                "recipe_digest": intent["recipe_digest"],
            }
        finally:
            cleanup(self.docker, self.directory, self.record, self.instance)
