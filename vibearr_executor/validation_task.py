"""Run exactly one image-owned check against the fresh candidate tree."""

import os
import hashlib
from pathlib import Path
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit

from .validation_profile import CHECK_IDS
from .artifacts import unpack

ROOT = Path(__file__).parent
WORK = Path("/work")


def environment() -> None:
    if any(
        name in os.environ
        for name in (
            "VIBEARR_BACKEND_SECRET_FILE",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        )
    ):
        raise RuntimeError("Validation cannot receive model or delivery credentials.")
    os.environ.update(
        DJANGO_SETTINGS_MODULE="vibearr_app.settings",
        DJANGO_DEBUG="True",
        TMPDIR="/work/.validation-tmp",
        PIP_CONFIG_FILE="/dev/null",
    )
    Path(os.environ["TMPDIR"]).mkdir(mode=0o700, exist_ok=True)
    database = os.environ.get("DATABASE_URL")
    if database:
        parsed = urlsplit(database)
        if parsed.scheme != "postgresql" or not parsed.hostname or not parsed.password:
            raise RuntimeError("A disposable PostgreSQL connection is required.")
        os.environ.update(
            VIBEARR_POSTGRES_DB=parsed.path[1:],
            VIBEARR_POSTGRES_USER=parsed.username or "",
            VIBEARR_POSTGRES_PASSWORD=parsed.password,
            VIBEARR_POSTGRES_HOST=parsed.hostname,
            VIBEARR_POSTGRES_PORT=str(parsed.port or 5432),
        )


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in CHECK_IDS:
        raise RuntimeError("Choose one registered validation check.")
    check = sys.argv[1]
    environment()
    python_files = [
        "/work/" + name
        for name in unpack(Path("/input/source.tar").read_bytes())
        if name.endswith(".py")
    ]
    if not python_files:
        raise RuntimeError(
            "The committed candidate contains no Python source to validate."
        )
    installed = Path("/opt/candidate/requirements.txt")
    if not installed.exists():
        installed = Path("/opt/validation/project-requirements.txt")
    if (
        hashlib.sha256((WORK / "requirements.txt").read_bytes()).digest()
        != hashlib.sha256(installed.read_bytes()).digest()
    ):
        raise RuntimeError(
            "Candidate requirements require a verified dependency artifact."
        )
    mypy_main: Any = None
    if check == "mypy":
        from mypy.main import main as mypy_main
        import mypy_django_plugin.main

        if not str(Path(mypy_django_plugin.main.__file__).resolve()).startswith(
            "/usr/local/lib/python3.12/site-packages/"
        ):
            raise RuntimeError(
                "The type-checking plugin must come from the trusted image."
            )
    os.chdir(WORK)
    dependencies = Path("/opt/candidate/venv/lib/python3.12/site-packages")
    if dependencies.exists():
        sys.path.insert(0, str(dependencies))
    sys.path.insert(0, str(WORK))
    commands = {
        "black": [
            "/usr/local/bin/python3.12",
            "-I",
            "-m",
            "black",
            "--config",
            str(ROOT / "validation_black.toml"),
            "--check",
            *python_files,
        ],
        "ruff": [
            "/usr/local/bin/ruff",
            "check",
            "--config",
            str(ROOT / "validation_ruff.toml"),
            "--no-cache",
            "--no-respect-gitignore",
            *python_files,
        ],
    }
    if check in commands:
        return subprocess.run(commands[check], check=False).returncode
    if check == "mypy":
        assert mypy_main is not None
        mypy_main(
            args=[
                *python_files,
                "--explicit-package-bases",
                "--config-file",
                str(ROOT / "validation_mypy.ini"),
                "--cache-dir",
                "/dev/null",
            ],
            clean_exit=True,
        )
        return 0
    return subprocess.run(
        [
            "/opt/application-python/bin/python",
            "-I",
            "-S",
            str(ROOT / "validation_app.py"),
            check,
        ],
        check=False,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
