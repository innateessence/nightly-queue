"""Resolve candidate requirements inside the credential-free setup boundary."""

import os
from pathlib import Path
import subprocess
import venv

from pip._vendor.packaging.requirements import Requirement


DIRECTORY = Path("/work/.vibearr-dependencies")


def main() -> None:
    if os.environ.get("VIBEARR_PHASE") != "setup" or os.environ.get(
        "VIBEARR_BACKEND_SECRET_FILE"
    ):
        raise RuntimeError("Dependency construction requires credential-free setup.")
    requirements = Path("/work/requirements.txt").read_text()
    lines = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(lines) > 256 or len(requirements.encode()) > 65536:
        raise RuntimeError("Candidate requirements exceed the configured build limit.")
    for line in lines:
        parsed = Requirement(line)
        if parsed.url:
            raise RuntimeError(
                "This build recipe supports index requirements, without URLs, includes, or pip options."
            )
    DIRECTORY.mkdir(mode=0o700)
    temporary = Path("/work/.vibearr-build-tmp")
    temporary.mkdir(mode=0o700)
    os.environ.update(
        TMPDIR=str(temporary),
        PIP_CONFIG_FILE="/dev/null",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    venv.EnvBuilder(with_pip=True, symlinks=False).create(DIRECTORY)
    linked = DIRECTORY / "lib64"
    if linked.is_symlink():
        linked.unlink()
    executable = DIRECTORY / "bin/python"
    command = [
        str(executable),
        "-I",
        "-m",
        "pip",
        "install",
        "--no-cache-dir",
        "--index-url",
        "https://pypi.org/simple",
        "-r",
        "/work/requirements.txt",
    ]
    subprocess.run(command, check=True)
    result = subprocess.run(
        [str(executable), "-I", "-m", "pip", "freeze", "--all"],
        check=True,
        capture_output=True,
    )
    (DIRECTORY / "resolved.txt").write_bytes(result.stdout)
    print("Candidate dependencies resolved inside the isolated setup task.", flush=True)


if __name__ == "__main__":
    main()
