import codecs
import os
import signal
import subprocess
import time
import threading

from .logs import Redactor


class TaskError(RuntimeError):
    pass


def stop_process(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=5)


class Commands:
    def __init__(self, log_path, timeout, secrets=(), on_output=None):
        self.log_path = log_path
        self.deadline = time.monotonic() + timeout
        self.secrets = [value for value in secrets if value]
        self.on_output = on_output

    def run(self, args, cwd, env, stdin=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TaskError("Task timeout exceeded.")
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        chunks = []
        errors = []
        reader = threading.Thread(
            target=self.read_output, args=(process, chunks, errors), daemon=True
        )
        writer = threading.Thread(
            target=self.write_input, args=(process, stdin), daemon=True
        )
        reader.start()
        writer.start()
        try:
            process.wait(timeout=remaining)
            reader.join(timeout=max(0, self.deadline - time.monotonic()))
            if reader.is_alive():
                raise subprocess.TimeoutExpired(args, remaining)
        except subprocess.TimeoutExpired:
            raise TaskError("Task timeout exceeded.") from None
        finally:
            stop_process(process)
            writer.join(timeout=5)
            reader.join(timeout=5)
        if errors:
            raise TaskError("Unable to capture command output.")
        text = "".join(chunks)
        if process.returncode:
            raise TaskError(f"Command failed: {args[0]}. See the private command log.")
        return text.strip()

    @staticmethod
    def write_input(process, text):
        try:
            if text is not None:
                process.stdin.write(text.encode())
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    def read_output(self, process, chunks, errors):
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        redactor = Redactor(self.secrets)
        try:
            with process.stdout, self.log_path.open("a") as log:
                while True:
                    data = os.read(process.stdout.fileno(), 4096)
                    text = redactor.feed(
                        decoder.decode(data, final=not data), final=not data
                    )
                    if text:
                        log.write(text)
                        log.flush()
                        chunks.append(text)
                        if self.on_output:
                            self.on_output(text)
                    if not data:
                        break
        except Exception:
            errors.append(True)


def clean_environment(home):
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    return {
        "PATH": str(home / ".local" / "bin") + os.pathsep + path,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Nightly Queue",
        "GIT_AUTHOR_EMAIL": "nightly-queue@localhost",
        "GIT_COMMITTER_NAME": "Nightly Queue",
        "GIT_COMMITTER_EMAIL": "nightly-queue@localhost",
    }
