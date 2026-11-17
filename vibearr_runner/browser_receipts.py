"""Source-bound evidence from the image-owned Docker browser job."""

from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit


BINDING_FIELDS = {
    "schema_version",
    "revision",
    "tree_sha",
    "source_digest",
    "application_image",
    "browser_image",
}
RECEIPT_FIELDS = BINDING_FIELDS | {
    "runtime",
    "browser",
    "url",
    "path",
    "sha256",
    "bytes",
    "captured_at",
    "viewport",
    "theme",
}
MAX_PNG = 32 * 1024**2
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1000},
    "mobile": {"width": 390, "height": 844},
}


def validate_binding(value):
    if not isinstance(value, dict) or set(value) != BINDING_FIELDS:
        raise ValueError("Supply the complete operator browser source binding.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("Unsupported browser source binding.")
    for key in ("revision", "tree_sha"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"[a-f0-9]{40}|[a-f0-9]{64}", value[key]
        ):
            raise ValueError("Browser source requires exact Git identities.")
    if len(value["revision"]) != len(value["tree_sha"]):
        raise ValueError("Browser commit and tree must use the same Git object format.")
    if not isinstance(value["source_digest"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", value["source_digest"]
    ):
        raise ValueError("Browser source archive digest is invalid.")
    for key in ("application_image", "browser_image"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"sha256:[a-f0-9]{64}", value[key]
        ):
            raise ValueError("Browser evidence requires actual immutable image IDs.")
    return value


def png_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 10000 <= info.st_size <= MAX_PNG
        ):
            raise ValueError("Select a bounded substantive PNG without links.")
        content = stream.read(MAX_PNG + 1)
    if len(content) > MAX_PNG or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Browser output is not a bounded PNG.")
    return content


def capture_receipt(binding, path, *, receipt_path, url, viewport, theme):
    validate_binding(binding)
    content = png_bytes(path)
    return {
        **binding,
        "schema_version": 2,
        "runtime": "docker-browser-v1",
        "browser": "playwright-chromium",
        "url": url,
        "path": str(receipt_path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "viewport": viewport,
        "theme": theme,
    }


def validate_receipt(
    value,
    expected_revision,
    *,
    alternate_revision=None,
    expected_tree=None,
    artifact_root=Path("/screenshots"),
):
    if not isinstance(value, dict) or set(value) != RECEIPT_FIELDS:
        raise ValueError(
            "Use complete Docker browser receipts; host screenshots cannot qualify this release."
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ValueError("Use source-bound Docker browser receipts.")
    validate_binding(
        {**{key: value[key] for key in BINDING_FIELDS}, "schema_version": 1}
    )
    if (
        value["runtime"] != "docker-browser-v1"
        or value["browser"] != "playwright-chromium"
    ):
        raise ValueError(
            "Screenshot does not identify the Docker browser implementation."
        )
    if value["revision"] not in {expected_revision, alternate_revision}:
        raise ValueError("Screenshot does not identify the accepted revision.")
    if expected_tree is not None and value["tree_sha"] != expected_tree:
        raise ValueError(
            "Screenshot was not rendered from the exact validated UI tree."
        )
    target = urlsplit(value["url"])
    if (
        target.scheme != "http"
        or target.hostname != "vibearr"
        or target.port != 8000
        or target.username
        or target.password
        or target.fragment
    ):
        raise ValueError("Screenshot must render the selected Docker application.")
    captured = datetime.fromisoformat(value["captured_at"])
    if (
        captured.utcoffset() is None
        or captured.timestamp() > datetime.now(timezone.utc).timestamp() + 5
    ):
        raise ValueError("Screenshot capture time is invalid.")
    path = Path(value["path"])
    try:
        relative = path.relative_to(artifact_root)
    except ValueError:
        raise ValueError(
            "Screenshot must be in the declared browser artifact volume."
        ) from None
    if (
        not path.is_absolute()
        or len(relative.parts) != 2
        or not re.fullmatch(r"capture-[a-f0-9]{32}", relative.parts[0])
    ):
        raise ValueError("Screenshot must identify a fresh Docker browser capture.")
    name = re.fullmatch(r"queue-(desktop|mobile)-(light|dark)\.png", relative.parts[1])
    if (
        name is None
        or value["viewport"] != VIEWPORTS[name[1]]
        or value["theme"] != name[2]
    ):
        raise ValueError("Screenshot viewport or theme differs from its capture.")
    if artifact_root.is_symlink() or path.parent.is_symlink():
        raise ValueError("Screenshot directories cannot be links.")
    content = png_bytes(path)
    if (
        type(value["bytes"]) is not int
        or value["bytes"] != len(content)
        or hashlib.sha256(content).hexdigest() != value["sha256"]
    ):
        raise ValueError("Screenshot artifact differs from its actual browser receipt.")
    return value


def store_receipts(state, run_id, frames):
    from .bootstrap_files import private_json
    from .journal import Journal
    from .native_workspace import atomic_bytes
    import json

    Journal(state)
    descriptor = os.open(
        state / "browser-acceptance.lock",
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        output = state / "browser-acceptance.json"
        records = private_json(output) if output.exists() else {}
        if not isinstance(records, dict) or len(records) > 1000:
            raise ValueError("Browser acceptance state is invalid or oversized.")
        if len(records) == 1000 and run_id not in records:
            raise ValueError(
                "Archive the completed browser acceptance state before collecting more runs."
            )
        if run_id in records and records[run_id] != frames:
            raise ValueError(
                "This run already has immutable browser receipts; preserve the original capture."
            )
        records[run_id] = frames
        atomic_bytes(output, json.dumps(records, sort_keys=True).encode())
    return {"run_id": run_id, "screenshots": len(frames), "path": str(output)}


def validate_capture_receipts(
    frames,
    expected_revision,
    *,
    alternate_revision=None,
    expected_tree=None,
    artifact_root=Path("/screenshots"),
):
    if not isinstance(frames, list) or len(frames) != 4:
        raise ValueError("Collect all desktop/mobile and light/dark captures.")
    expected = {(name, theme) for name in VIEWPORTS for theme in ("light", "dark")}
    observed = set()
    first = frames[0]
    for frame in frames:
        validate_receipt(
            frame,
            expected_revision,
            alternate_revision=alternate_revision,
            expected_tree=expected_tree,
            artifact_root=artifact_root,
        )
        if any(frame[key] != first[key] for key in BINDING_FIELDS):
            raise ValueError("Browser frames must share one source/image binding.")
        if Path(frame["path"]).parent != Path(first["path"]).parent:
            raise ValueError("Browser frames must belong to one capture report.")
        name = "desktop" if frame["viewport"] == VIEWPORTS["desktop"] else "mobile"
        observed.add((name, frame["theme"]))
    if observed != expected:
        raise ValueError("Browser capture coverage is incomplete or duplicated.")
    return frames


def collect_report(report_path, run_id, state, *, artifact_root=Path("/screenshots")):
    from uuid import UUID
    from .bootstrap_files import private_json

    if str(UUID(run_id)) != run_id:
        raise ValueError("Select the actual accepted run UUID.")
    relative = report_path.relative_to(artifact_root)
    if (
        len(relative.parts) != 2
        or not re.fullmatch(r"capture-[a-f0-9]{32}", relative.parts[0])
        or relative.parts[1] != "browser.json"
    ):
        raise ValueError(
            "Select a source-bound report from the browser artifact volume."
        )
    if artifact_root.is_symlink() or report_path.parent.is_symlink():
        raise ValueError("Browser report directories cannot be links.")
    report = private_json(report_path, 1024**2)
    if (
        type(report.get("schema_version")) is not int
        or report["schema_version"] != 2
        or report.get("kind") != "docker-browser-acceptance"
    ):
        raise ValueError(
            "Unbound browser smoke output cannot qualify native acceptance."
        )
    if report.get("page_errors") != [] or report.get("failed_requests") != []:
        raise ValueError("Browser errors prevent acceptance.")
    binding = validate_binding(report["source_binding"])
    frames = validate_capture_receipts(
        report["screenshots"],
        binding["revision"],
        expected_tree=binding["tree_sha"],
        artifact_root=artifact_root,
    )
    for frame in frames:
        if any(
            frame[key] != binding[key] for key in BINDING_FIELDS - {"schema_version"}
        ):
            raise ValueError(
                "Browser frame differs from the inspected source/image binding."
            )
        if Path(frame["path"]).parent != report_path.parent:
            raise ValueError("Browser frames must belong to this capture report.")
    return store_receipts(state, run_id, frames)


def adopt_capture(name, *, artifact_root=Path("/artifacts"), uid=10001):
    if not re.fullmatch(r"capture-[a-f0-9]{32}", name):
        raise ValueError("Select one complete browser capture directory.")
    capture = artifact_root / name
    if artifact_root.is_symlink() or capture.is_symlink() or not capture.is_dir():
        raise ValueError("Browser import directories cannot be links.")
    expected = {"browser.json"} | {
        f"queue-{view}-{theme}.png" for view in VIEWPORTS for theme in ("light", "dark")
    }
    paths = list(capture.iterdir())
    if {path.name for path in paths} != expected:
        raise ValueError(
            "Import exactly one complete browser report and its four PNGs."
        )
    for path in paths:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Browser imports cannot contain links or special files.")
        if path.name == "browser.json":
            if info.st_size > 1024**2:
                raise ValueError("Browser import report is oversized.")
        else:
            png_bytes(path)
    for path in paths:
        os.chown(path, uid, uid, follow_symlinks=False)
        path.chmod(0o600)
    os.chown(capture, uid, uid, follow_symlinks=False)
    capture.chmod(0o700)
    return {
        "report_path": str(Path("/screenshots") / name / "browser.json"),
        "imported": True,
    }


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adopt-capture")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--state", type=Path, default=Path("/var/lib/vibearr"))
    options = parser.parse_args()
    if options.adopt_capture:
        if options.report or options.run_id:
            parser.error("Import a capture before binding its receipts to a run.")
        print(json.dumps(adopt_capture(options.adopt_capture)))
        return
    if not options.report or not options.run_id:
        parser.error("Select --report and the actual --run-id.")
    print(json.dumps(collect_report(options.report, options.run_id, options.state)))


if __name__ == "__main__":
    main()
