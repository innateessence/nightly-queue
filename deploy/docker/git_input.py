"""Read exact Git objects without importing checkout configuration or hooks."""

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


SHA = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")


def revision(root):
    value = (root / "HEAD").read_text().strip()
    if SHA.fullmatch(value):
        return value
    if not value.startswith("ref: refs/"):
        raise ValueError("Git input HEAD is not a canonical object or reference.")
    reference = value[5:]
    if not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", reference) or ".." in reference:
        raise ValueError("Git input reference is unsafe.")
    path = root / reference
    if path.is_file():
        value = path.read_text().strip()
    else:
        matches = [
            line.split()[0]
            for line in (root / "packed-refs").read_text().splitlines()
            if line.endswith(" " + reference)
        ]
        value = matches[0] if len(matches) == 1 else ""
    if not SHA.fullmatch(value):
        raise ValueError("Git input reference has no exact object identity.")
    return value


def validate(root):
    total, count = 0, 0
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(parent) / name
            info = path.lstat()
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError("Git input cannot contain links or special files.")
            if info.st_nlink != 1 and stat.S_ISREG(info.st_mode):
                raise ValueError("Git input cannot contain hard-linked files.")
            total += info.st_size if stat.S_ISREG(info.st_mode) else 0
            count += 1
            if total > 2 * 1024**3 or count > 250000:
                raise ValueError("Git input exceeds its fixed bounds.")
    if (root / "objects/info/alternates").exists() or (
        root / "objects/info/http-alternates"
    ).exists():
        raise ValueError("Git input cannot reference another object store.")
    if set(path.name for path in root.iterdir()) != {
        "HEAD",
        "objects",
        "refs",
        "packed-refs",
    }:
        raise ValueError("Git input may contain only objects and reference metadata.")


def repository(root, target, selected=None):
    validate(root)
    selected = selected or revision(root)
    if not SHA.fullmatch(selected):
        raise ValueError("Select an exact candidate commit identity.")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(
        [
            "git",
            "init",
            "--bare",
            "--object-format=" + ("sha256" if len(selected) == 64 else "sha1"),
            str(target),
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    shutil.rmtree(target / "objects")
    (target / "objects").symlink_to(root / "objects", target_is_directory=True)
    return target, selected


def ancestry(checkout, base, introduction):
    if not base and not introduction:
        return
    if (
        not base
        or not introduction
        or not SHA.fullmatch(base)
        or not SHA.fullmatch(introduction)
    ):
        raise ValueError(
            "Bootstrap ancestry requires exact base and introduction identities."
        )
    subprocess.run(
        ["git", "-C", str(checkout), "merge-base", "--is-ancestor", base, introduction],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )


def freeze_worktree(root, target):
    from vibearr_executor.artifacts import extract, snapshot
    from vibearr_runner.native_validation import CandidateSource

    extract(snapshot(root), target)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    command = [
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "user.name=Container checks",
        "-c",
        "user.email=checks@invalid",
        "-C",
        str(target),
    ]
    for arguments in (
        ("init", "-q"),
        ("add", "."),
        ("commit", "-qm", "Freeze disposable check snapshot"),
    ):
        subprocess.run(
            [*command, *arguments], check=True, capture_output=True, env=environment
        )
    commit = (
        subprocess.check_output([*command, "rev-parse", "HEAD"], env=environment)
        .decode()
        .strip()
    )
    return CandidateSource.from_git(target, commit)
