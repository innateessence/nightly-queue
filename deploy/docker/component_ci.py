"""Assemble an exact component head with explicitly pinned prerequisites."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile


BASE_SHA = "b03776d52a43532e85b2dbca72d3ed7b58beec8e"
BASE_TREE = "45216068e50d933e3540b4a4a2c5524a99503c1b"
DEPENDENCY_SHA = "25a3c786d977facb304868dcfd94d3901a916df1"
DEPENDENCY_TREE = "1f36d99a28b64aacfd054aa9a3246af289f9a709"
REPOSITORY = "innateessence/nightly-queue"
SHA = re.compile(r"[a-f0-9]{40}")
MODES = {"100644", "100755"}
MANIFEST_BYTES = 4 * 1024**2
PR_NUMBER = re.compile(r"[1-9][0-9]{0,8}")


class Rejected(ValueError):
    pass


def identity(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise Rejected("Select a complete lowercase Git object identity.")
    return value


def source_path(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise Rejected("Source path is missing or exceeds its bound.")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(
        part in {"", ".", "..", ".git"} for part in parts
    ):
        raise Rejected("Source path escapes the source tree.")
    if any(character in value for character in ("\\", "\x00", "\r", "\n")):
        raise Rejected("Source path has unsupported separators or control characters.")
    return value


def manifest_source(value):
    source_path(value)
    prefix = ".github/component-prerequisites/pr-"
    if not value.startswith(prefix) or not value.endswith(".json"):
        raise Rejected("Select the unique manifest path for the external PR number.")
    number = value[len(prefix) : -5]
    if not PR_NUMBER.fullmatch(number):
        raise Rejected(
            "Manifest PR number must be a canonical positive integer of at most nine digits."
        )
    return value


class Git:
    def __init__(self, directory):
        self.directory = Path(directory)

    def run(self, *arguments, data=None, commit_time=None):
        environment = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": "/dev/null",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
        }
        if commit_time is not None:
            if not re.fullmatch(r"[0-9]{1,12}", commit_time):
                raise Rejected("Candidate commit time is invalid.")
            environment["GIT_AUTHOR_DATE"] = "@" + commit_time + " +0000"
            environment["GIT_COMMITTER_DATE"] = environment["GIT_AUTHOR_DATE"]
        result = subprocess.run(
            ["git", "-C", str(self.directory), *arguments],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise Rejected(
                "Git rejected the selected source operation: "
                + result.stderr.decode(errors="replace")[:2000]
            )
        return result.stdout

    def tree(self, revision):
        entries = {}
        for record in self.run("ls-tree", "-rz", identity(revision)).split(b"\0"):
            if not record:
                continue
            header, raw_path = record.split(b"\t", 1)
            mode, kind, blob = header.decode().split()
            path = source_path(raw_path.decode("utf-8"))
            if mode not in MODES or kind != "blob":
                raise Rejected(
                    "Symlinks, submodules and special source modes are unsupported: "
                    + path
                )
            entries[path] = {"mode": mode, "type": kind, "blob": identity(blob)}
        if len(entries) > 25000:
            raise Rejected("Source tree exceeds the fixed file-count bound.")
        return entries


def changed_paths(base, candidate):
    return {
        path
        for path in base.keys() | candidate.keys()
        if base.get(path) != candidate.get(path)
    }


def prerequisite_entries(base, candidate, dependency):
    protected = changed_paths(base, candidate)
    paths = (candidate.keys() | dependency.keys()) - protected
    return [
        {
            "path": path,
            **dependency.get(path, {"mode": None, "type": None, "blob": None}),
        }
        for path in sorted(paths)
        if candidate.get(path) != dependency.get(path)
    ]


def generate_manifest(git, candidate):
    base, current, dependency = (
        git.tree(revision) for revision in (BASE_SHA, candidate, DEPENDENCY_SHA)
    )
    return {
        "schema_version": 1,
        "repository": REPOSITORY,
        "base_sha": BASE_SHA,
        "base_tree": BASE_TREE,
        "dependency_sha": DEPENDENCY_SHA,
        "dependency_tree": DEPENDENCY_TREE,
        "prerequisites": prerequisite_entries(base, current, dependency),
    }


def parse_manifest(value):
    fields = {
        "schema_version",
        "repository",
        "base_sha",
        "base_tree",
        "dependency_sha",
        "dependency_tree",
        "prerequisites",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise Rejected("Prerequisite manifest has unknown or missing fields.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise Rejected("Unsupported prerequisite manifest version.")
    if (
        value["repository"] != REPOSITORY
        or value["base_sha"] != BASE_SHA
        or value["dependency_sha"] != DEPENDENCY_SHA
        or value["base_tree"] != BASE_TREE
        or value["dependency_tree"] != DEPENDENCY_TREE
    ):
        raise Rejected(
            "Prerequisite source identities differ from the reviewed immutable pins."
        )
    entries = value["prerequisites"]
    if not isinstance(entries, list) or len(entries) > 25000:
        raise Rejected("Prerequisite path list is invalid or exceeds its bound.")
    paths = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "mode",
            "type",
            "blob",
        }:
            raise Rejected("Prerequisite path record has unknown or missing fields.")
        paths.append(source_path(entry["path"]))
        if entry["mode"] is None and entry["type"] is None and entry["blob"] is None:
            continue
        if entry["mode"] not in MODES or entry["type"] != "blob":
            raise Rejected("Prerequisite source mode is unsupported.")
        identity(entry["blob"])
    if paths != sorted(set(paths)):
        raise Rejected("Prerequisite paths must be unique and sorted.")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Rejected("Prerequisite manifest contains a duplicate JSON key.")
        result[key] = value
    return result


def read_manifest(path):
    with path.open("rb") as stream:
        content = stream.read(MANIFEST_BYTES + 1)
    if len(content) > MANIFEST_BYTES:
        raise Rejected("Prerequisite manifest exceeds its fixed byte bound.")
    value = json.loads(content, object_pairs_hook=unique_pairs)
    return content, parse_manifest(value)


def blob_identity(content):
    header = b"blob " + str(len(content)).encode() + b"\0"
    return hashlib.sha1(header + content).hexdigest()


def verify_control_inputs(candidate, supplied):
    for path, content in supplied.items():
        expected = candidate.get(path)
        if expected is None or expected["blob"] != blob_identity(content):
            raise Rejected("CI control input is not the exact candidate blob: " + path)


def assemble(git, candidate_sha, manifest, *, supplied=None):
    identity(candidate_sha)
    parse_manifest(manifest)
    base, candidate, dependency = (
        git.tree(revision) for revision in (BASE_SHA, candidate_sha, DEPENDENCY_SHA)
    )
    if (
        git.run("rev-parse", BASE_SHA + "^{tree}").decode().strip() != BASE_TREE
        or git.run("rev-parse", DEPENDENCY_SHA + "^{tree}").decode().strip()
        != DEPENDENCY_TREE
    ):
        raise Rejected("Pinned commit tree differs from its recorded immutable tree.")
    git.run("merge-base", "--is-ancestor", BASE_SHA, candidate_sha)
    git.run("merge-base", "--is-ancestor", BASE_SHA, DEPENDENCY_SHA)
    required = prerequisite_entries(base, candidate, dependency)
    if manifest["prerequisites"] != required:
        raise Rejected(
            "Prerequisite allowlist differs from the complete unowned donor changes."
        )
    verify_control_inputs(candidate, supplied or {})
    if any(path.name != ".git" for path in git.directory.iterdir()):
        raise Rejected("Source assembly requires a fresh object-only repository.")
    protected = changed_paths(base, candidate)
    expected = dict(candidate)
    git.run("read-tree", candidate_sha)
    for entry in required:
        path = entry["path"]
        if path in protected:
            raise Rejected("Prerequisite attempts to override a candidate-owned path.")
        if entry["mode"] is None:
            git.run("update-index", "--force-remove", "--", path)
            expected.pop(path, None)
        else:
            git.run(
                "update-index",
                "--add",
                "--cacheinfo",
                f"{entry['mode']},{entry['blob']},{path}",
            )
            expected[path] = {
                "mode": entry["mode"],
                "type": "blob",
                "blob": entry["blob"],
            }
    tree = git.run("write-tree").decode().strip()
    actual = git.tree(tree)
    if actual != expected or any(
        actual.get(path) != candidate.get(path) for path in protected
    ):
        raise Rejected(
            "Derived tree masks candidate changes or contains undeclared dependency changes."
        )
    message = f"Component {candidate_sha} with pinned prerequisites {DEPENDENCY_SHA}\n"
    parents = [candidate_sha] + (
        [DEPENDENCY_SHA] if candidate_sha != DEPENDENCY_SHA else []
    )
    arguments = [
        "-c",
        "user.name=Component CI",
        "-c",
        "user.email=component-ci@example.invalid",
        "commit-tree",
        tree,
    ]
    for parent in parents:
        arguments.extend(("-p", parent))
    commit_time = (
        git.run("show", "--no-patch", "--format=%ct", candidate_sha).decode().strip()
    )
    derived = (
        git.run(*arguments, data=message.encode(), commit_time=commit_time)
        .decode()
        .strip()
    )
    git.run("update-ref", "refs/heads/ci-derived", derived)
    git.run("symbolic-ref", "HEAD", "refs/heads/ci-derived")
    git.run("checkout-index", "--all", "--force")
    protected_records = [
        {"path": path, "candidate": candidate.get(path), "derived": actual.get(path)}
        for path in sorted(protected)
    ]
    return {
        "schema_version": 1,
        "scope": "Actual component head plus explicitly pinned prerequisite integration; not standalone-head release qualification.",
        "candidate_sha": candidate_sha,
        "candidate_tree": git.run("rev-parse", candidate_sha + "^{tree}")
        .decode()
        .strip(),
        "base_sha": BASE_SHA,
        "base_tree": BASE_TREE,
        "dependency_sha": DEPENDENCY_SHA,
        "dependency_tree": git.run("rev-parse", DEPENDENCY_SHA + "^{tree}")
        .decode()
        .strip(),
        "derived_commit": derived,
        "derived_tree": tree,
        "parents": parents,
        "manifest_sha256": hashlib.sha256(canonical(manifest)).hexdigest(),
        "candidate_paths": protected_records,
        "prerequisites": required,
        "entire_tree_verified": True,
        "candidate_paths_preserved": True,
    }


def read_input_head(directory):
    value = (directory / "HEAD").read_text().strip()
    if value.startswith("ref: refs/"):
        name = value[5:]
        if not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", name) or ".." in name:
            raise Rejected("Input HEAD reference is unsafe.")
        path = directory / name
        if path.is_file():
            value = path.read_text().strip()
        else:
            matching = [
                line.split()[0]
                for line in (directory / "packed-refs").read_text().splitlines()
                if line.endswith(" " + name)
            ]
            if len(matching) != 1:
                raise Rejected("Input HEAD reference is missing or ambiguous.")
            value = matching[0]
    return identity(value)


def validate_input(directory):
    if set(path.name for path in directory.iterdir()) != {
        "HEAD",
        "objects",
        "refs",
        "packed-refs",
    }:
        raise Rejected(
            "Only Git objects and reference metadata may enter the source image."
        )
    count, total = 0, 0
    for parent, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            path = Path(parent) / name
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise Rejected("Git metadata contains a link or special file.")
            if path.is_file():
                info = path.stat()
                if info.st_nlink != 1:
                    raise Rejected("Git metadata contains a hard link.")
                total += info.st_size
            count += 1
            if total > 2 * 1024**3 or count > 250000:
                raise Rejected("Git metadata exceeds the fixed input bounds.")
    if any(
        (directory / ("objects/info/" + name)).exists()
        for name in ("alternates", "http-alternates")
    ):
        raise Rejected("Git metadata references an external object store.")


@contextmanager
def input_repository(directory, candidate_sha):
    directory = Path(directory)
    validate_input(directory)
    if read_input_head(directory) != identity(candidate_sha):
        raise Rejected("Captured Git HEAD differs from the workflow candidate SHA.")
    with tempfile.TemporaryDirectory(prefix="component-objects-") as temporary:
        git = Git(temporary)
        git.run("init", "--quiet")
        shutil.copytree(
            directory / "objects", git.directory / ".git/objects", dirs_exist_ok=True
        )
        for revision in (candidate_sha, BASE_SHA, DEPENDENCY_SHA):
            try:
                git.run("cat-file", "-e", revision + "^{commit}")
            except Rejected:
                raise Rejected(
                    "A pinned source object is missing; capture full Git history in a Docker Git job before offline assembly."
                ) from None
        yield git


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("manifest", "assemble"))
    parser.add_argument("--candidate", required=True, type=identity)
    parser.add_argument("--input", type=Path, default=Path("/git-input"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("/component-prerequisites.json")
    )
    parser.add_argument("--output", type=Path, default=Path("/output"))
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--write-manifest", type=Path)
    parser.add_argument("--manifest-source", type=manifest_source)
    args = parser.parse_args()
    if args.repository is not None:
        if args.action != "manifest":
            raise Rejected(
                "A working repository is supported only for manifest generation."
            )
        git = Git(args.repository)
        if git.run("rev-parse", "HEAD").decode().strip() != args.candidate:
            raise Rejected("Manifest source HEAD differs from the selected candidate.")
        value = json.dumps(generate_manifest(git, args.candidate), indent=2) + "\n"
        if args.write_manifest:
            args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
            args.write_manifest.write_text(value)
        else:
            print(value, end="")
        return
    with input_repository(args.input, args.candidate) as git:
        if args.action == "manifest":
            value = json.dumps(generate_manifest(git, args.candidate), indent=2) + "\n"
            if args.write_manifest:
                args.write_manifest.parent.mkdir(parents=True, exist_ok=True)
                args.write_manifest.write_text(value)
            else:
                print(value, end="")
            return
        if args.output.exists() and any(args.output.iterdir()):
            raise Rejected("Derived source output must be empty.")
        args.output.mkdir(parents=True, exist_ok=True)
        if args.manifest_source is None:
            raise Rejected(
                "Assembly requires the unique externally selected PR manifest source."
            )
        supplied_manifest, manifest = read_manifest(args.manifest)
        supplied = {
            args.manifest_source: supplied_manifest,
            "deploy/docker/component_ci.py": Path(__file__).read_bytes(),
            "deploy/docker/component-ci.sh": Path("/component-ci.sh").read_bytes(),
            "deploy/docker/Component.Dockerfile": Path(
                "/Component.Dockerfile"
            ).read_bytes(),
        }
        report = assemble(git, args.candidate, manifest, supplied=supplied)
        report["manifest_source"] = args.manifest_source
        report["component_pr_number"] = int(
            args.manifest_source.rsplit("pr-", 1)[1][:-5]
        )
        shutil.copytree(git.directory, args.output / "repo")
        (args.output / "assembly.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    key: report[key]
                    for key in (
                        "candidate_sha",
                        "dependency_sha",
                        "derived_commit",
                        "derived_tree",
                    )
                }
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (Rejected, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
