"""Immutable image entrypoint. PID 1 alone retains setup/termination capabilities."""

import json
import os
import select
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .artifacts import extract, result_manifest, snapshot, unpack
from .contracts import MAX_LOG, canonical, digest
from .secret_material import contains_secret, protection_material, redact


def save(path: Path, value: bytes) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def redact_logs(content: bytes, secret: bytes) -> bytes:
    return redact(content, secret, incomplete=True)


def redact_result(value, secret: str):
    if isinstance(value, str):
        return redact(value.encode(), secret.encode()).decode()
    if isinstance(value, dict):
        return {key: redact_result(item, secret) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_result(item, secret) for item in value]
    return value


def terminate_children() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        children = []
        for path in Path("/proc").iterdir():
            if not path.name.isdigit():
                continue
            try:
                status = (path / "status").read_text()
                uid = next(
                    line.split()[1]
                    for line in status.splitlines()
                    if line.startswith("Uid:")
                )
                if uid == "1000":
                    children.append(int(path.name))
            except (FileNotFoundError, ProcessLookupError):
                continue
        for child in children:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if not children:
            return
        time.sleep(0.01)
    raise RuntimeError("Task descendants did not terminate.")


def main() -> None:
    state = Path("/state")
    job = json.loads(Path("/input/job.json").read_bytes())
    deadline = datetime.fromisoformat(job["manifest"]["deadline"]).timestamp()
    secret = Path("/input/secret").read_bytes()
    protected_values = protection_material(secret, job.get("database_url", ""))
    source = Path("/input/source.tar").read_bytes()
    save(state / "booted", b"ready")
    remaining = max(0, min(3600, deadline - time.time()))
    expires = time.monotonic() + remaining
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    result = {
        "schema_version": 1,
        "run_id": job["manifest"]["run_id"],
        "operation_id": job["manifest"]["operation_id"],
        "exit_code": None,
        "timed_out": False,
        "terminated": False,
    }
    logs = bytearray()
    log_received = 0
    try:
        extract(source, Path("/work"))
        for parent, directories, names in os.walk("/work"):
            for name in directories + names:
                os.chown(os.path.join(parent, name), 1000, 1000)
        os.chown("/work", 1000, 1000)
        while (
            not (state / "go").exists() and not stopping and time.monotonic() < expires
        ):
            time.sleep(0.05)
        if stopping or time.monotonic() >= expires:
            raise TimeoutError("Launch gate did not open before deadline.")
        environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/task",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "VIBEARR_CONTEXT": "/input/context",
            "VIBEARR_PHASE": job["profile"]["phase"],
        }
        if job.get("proxy"):
            environment.update(
                HTTPS_PROXY=job["proxy"],
                HTTP_PROXY=job["proxy"],
                https_proxy=job["proxy"],
                http_proxy=job["proxy"],
            )
        if job.get("database_url"):
            environment["DATABASE_URL"] = job["database_url"]
            environment["VIBEARR_DISPOSABLE_DATABASE_URL"] = job["database_url"]

        def demote():
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)
            os.umask(0o077)

        subprocess.run(
            ["git", "-c", "init.defaultBranch=task", "init", "-q", "/work"],
            env=environment,
            preexec_fn=demote,
            check=True,
        )
        if secret:
            path = Path("/secrets/backend")
            path.write_bytes(secret)
            path.chmod(0o400)
            os.chown(path, 1000, 1000)
            environment["VIBEARR_BACKEND_SECRET_FILE"] = str(path)
            environment[job["profile"]["backend_secret_env"]] = secret.decode()

        process = subprocess.Popen(
            job["profile"]["argv"],
            cwd="/work",
            env=environment,
            preexec_fn=demote,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        result["started_at"] = time.time()
        assert process.stdout is not None
        os.set_blocking(process.stdout.fileno(), False)
        while process.poll() is None and not stopping and time.monotonic() < expires:
            ready, _, _ = select.select([process.stdout], [], [], 0.1)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                log_received += len(chunk)
                logs.extend(chunk[: max(0, MAX_LOG - len(logs))])
                save(state / "logs.bin", redact_logs(bytes(logs), protected_values))
        result["timed_out"] = time.monotonic() >= expires
        result["canceled"] = stopping
        if process.poll() is None:
            process.kill()
        result["exit_code"] = process.wait()
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            log_received += len(chunk)
            logs.extend(chunk[: max(0, MAX_LOG - len(logs))])
    except Exception as exc:
        result["error"] = type(exc).__name__ + ": " + str(exc)[:500]
    finally:
        terminate_children()
        result["terminated"] = True
        result["ended_at"] = time.time()
        resources = {}
        for name in ("memory.events", "pids.events", "cpu.stat"):
            path = Path("/sys/fs/cgroup") / name
            resources[name] = {
                line.split()[0]: int(line.split()[1])
                for line in path.read_text().splitlines()
            }
        resources["pids.max"] = {
            "value": int(Path("/sys/fs/cgroup/pids.max").read_text())
        }
        for name in ("memory.max", "memory.swap.max"):
            resources[name] = {
                "value": int((Path("/sys/fs/cgroup") / name).read_text())
            }
        result["resources"] = resources
        Path("/secrets/backend").unlink(missing_ok=True)
        from .validation_profile import BUILD_ARGV

        if (
            tuple(job["profile"]["argv"]) == BUILD_ARGV
            and job["profile"]["phase"] == "setup"
        ):
            try:
                from .dependency_artifacts import capture, manifest

                if (
                    result.get("exit_code") == 0
                    and not result.get("timed_out")
                    and not result.get("canceled")
                ):
                    requirements = unpack(source)["requirements.txt"][1]
                    dependencies = capture(
                        Path("/work/.vibearr-dependencies"), requirements
                    )
                    save(state / "dependencies.tar", dependencies)
                    result["dependency_artifact"] = manifest(dependencies)
            except Exception as exc:
                result["dependency_error"] = type(exc).__name__ + ": " + str(exc)[:500]
            finally:
                for temporary in (".vibearr-dependencies", ".vibearr-build-tmp"):
                    target = Path("/work") / temporary
                    if target.is_symlink():
                        target.unlink()
                    elif target.is_dir():
                        shutil.rmtree(target)
        try:
            exported = snapshot(Path("/work"))
            if contains_secret(exported, protected_values):
                raise ValueError(
                    "Workspace contains the selected backend credential or disposable database credentials; source export withheld."
                )
            save(state / "source.tar", exported)
            result["artifact"] = result_manifest(source, exported)
        except Exception as exc:
            result["export_error"] = type(exc).__name__ + ": " + str(exc)[:500]
        raw_logs = redact_logs(bytes(logs), protected_values)
        if protected_values:
            result = redact_result(result, protected_values.decode())
        save(state / "logs.bin", raw_logs)
        result["log_digest"] = digest(raw_logs)
        result["log_bytes"] = len(raw_logs)
        result["log_truncated"] = log_received > MAX_LOG
        save(state / "result.json", canonical(result))
    while True:
        time.sleep(0.1)


if __name__ == "__main__":
    main()
