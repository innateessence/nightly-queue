import json
import os
import selectors
import subprocess
import time

from .contracts import Rejected


class EngineError(RuntimeError):
    pass


class Docker:
    def __init__(self, executable: str = "docker"):
        self.executable = executable

    def run(
        self, *arguments: str, limit: int = 2 * 1024 * 1024, timeout: int = 60
    ) -> bytes:
        process = subprocess.Popen(
            [self.executable, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        output, errors = bytearray(), bytearray()
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise EngineError(
                        "Engine response timed out; reconcile the recorded operation."
                    )
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = output if key.fileobj is process.stdout else errors
                    target.extend(chunk)
                    if len(target) > limit:
                        raise EngineError("Engine response exceeded its byte bound.")
            if process.wait(timeout=5):
                raise EngineError(errors.decode(errors="replace")[:1000])
            return bytes(output)
        finally:
            selector.close()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()

    def inspect(self, name: str) -> dict | None:
        try:
            return json.loads(self.run("container", "inspect", name))[0]
        except EngineError as exc:
            if "No such container" in str(exc):
                return None
            raise

    def read(self, container: str, filename: str, limit: int) -> bytes | None:
        if filename not in {
            "booted",
            "result.json",
            "logs.bin",
            "source.tar",
            "dependencies.tar",
        }:
            raise Rejected("Unknown result file.")
        try:
            code = "import pathlib,sys;sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())"
            return self.run(
                "exec",
                "--user",
                "0:0",
                container,
                "python",
                "-I",
                "-c",
                code,
                "/state/" + filename,
                limit=limit,
            )
        except EngineError as exc:
            if "FileNotFoundError" in str(exc):
                return None
            raise

    def remove(self, kind: str, name: str) -> None:
        arguments = (
            ("rm", "-f", name) if kind == "container" else ("network", "rm", name)
        )
        try:
            self.run(*arguments)
        except EngineError as exc:
            if "No such" not in str(exc) and "not found" not in str(exc).lower():
                raise
