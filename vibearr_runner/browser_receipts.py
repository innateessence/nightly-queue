"""Source-bound evidence from the image-owned Docker browser job."""

from datetime import datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
from urllib.parse import urlsplit
from uuid import UUID
import zlib


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
WORKER_BINDING_FIELDS = {
    "run_id",
    "operation_id",
    "execution_identity",
    "profile_id",
    "profile_digest",
}
MAX_PNG = 32 * 1024**2
VIEWPORTS = {
    "desktop": {"width": 1440, "height": 1000},
    "mobile": {"width": 390, "height": 844},
}


def validate_page_path(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"/|/activity/|/runner/runs/|/runner/runs/[a-f0-9-]{36}/|/projects/[1-9][0-9]{0,9}/edit/",
            value,
        )
        is None
    ):
        raise ValueError("Select a registered local browser application page.")
    if value.startswith("/runner/runs/") and value != "/runner/runs/":
        identity = value.removeprefix("/runner/runs/").removesuffix("/")
        if str(UUID(identity)) != identity:
            raise ValueError("Select one canonical local run page.")
    return value


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
    validate_png(content)
    return content


def validate_png(content):
    offset, compressed, dimensions, ended = 8, bytearray(), None, False
    while offset < len(content):
        if offset + 12 > len(content):
            raise ValueError("Browser PNG has truncated chunks.")
        size = int.from_bytes(content[offset : offset + 4], "big")
        kind = content[offset + 4 : offset + 8]
        end = offset + 12 + size
        if end > len(content):
            raise ValueError("Browser PNG has truncated chunks.")
        payload = content[offset + 8 : end - 4]
        checksum = int.from_bytes(content[end - 4 : end], "big")
        if zlib.crc32(kind + payload) != checksum:
            raise ValueError("Browser PNG chunk integrity failed.")
        if dimensions is None:
            if kind != b"IHDR" or size != 13:
                raise ValueError("Browser PNG is missing its image header.")
            width, height, depth, color, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", payload)
            )
            if (
                not 1 <= width <= 10000
                or not 1 <= height <= 30000
                or width * height > 16000000
                or depth != 8
                or color not in {2, 6}
                or compression
                or filtering
                or interlace
            ):
                raise ValueError("Browser PNG dimensions or encoding are unsupported.")
            dimensions = (width, height, 3 if color == 2 else 4)
        elif kind == b"IHDR":
            raise ValueError("Browser PNG repeats its image header.")
        if kind == b"IDAT":
            compressed.extend(payload)
        if kind == b"IEND":
            ended = size == 0 and end == len(content)
            break
        offset = end
    if not ended or dimensions is None:
        raise ValueError("Browser PNG has no complete image end.")
    width, height, channels = dimensions
    expected = (width * channels + 1) * height
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, expected + 1)
    except zlib.error as error:
        raise ValueError("Browser PNG pixels cannot be decoded.") from error
    if (
        len(raw) != expected
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise ValueError("Browser PNG pixel data differs from its dimensions.")
    if any(raw[row] > 4 for row in range(0, len(raw), width * channels + 1)):
        raise ValueError("Browser PNG scanline filter is invalid.")
    return width, height


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


def validate_worker_binding(value):
    if not isinstance(value, dict) or set(value) != WORKER_BINDING_FIELDS:
        raise ValueError("Supply the complete worker browser operation binding.")
    for field in ("run_id", "operation_id"):
        if not isinstance(value[field], str) or str(UUID(value[field])) != value[field]:
            raise ValueError("Browser operation identities must be canonical UUIDs.")
    identity = value["execution_identity"]
    if not isinstance(identity, dict) or set(identity) != {
        "runtime",
        "protocol_version",
        "instance_id",
        "worker_boot",
        "binding_digest",
    }:
        raise ValueError("Browser evidence requires the actual worker identity.")
    if (
        identity["runtime"] != "compact-worker-v1"
        or type(identity["protocol_version"]) is not int
    ):
        raise ValueError("Browser evidence requires the compact worker runtime.")
    if identity["protocol_version"] != 2:
        raise ValueError("Unsupported worker browser protocol.")
    for field in ("instance_id", "worker_boot"):
        if (
            not isinstance(identity[field], str)
            or str(UUID(identity[field])) != identity[field]
        ):
            raise ValueError("Worker browser boot identities must be canonical UUIDs.")
    for digest in (identity["binding_digest"], value["profile_digest"]):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Browser operation digests must be SHA-256 values.")
    if value["profile_id"] != "browser-ui":
        raise ValueError("Browser evidence requires the registered UI recipe.")
    return value


def validate_capture_mode(value):
    if not isinstance(value, dict) or set(value) != {"focus", "full_page"}:
        raise ValueError("Declare the browser capture focus and extent.")
    if not isinstance(value["focus"], str) or type(value["full_page"]) is not bool:
        raise ValueError("Browser capture focus and extent are invalid.")
    if (value["focus"], value["full_page"]) not in {("page", True), ("factory", False)}:
        raise ValueError("Select the fixed full-page or Factory viewport recipe.")
    return value


def capture_worker_receipt(
    binding, worker_binding, path, *, focus="page", full_page=True, **options
):
    validate_worker_binding(worker_binding)
    mode = validate_capture_mode({"focus": focus, "full_page": full_page})
    result = capture_receipt(binding, path, **options)
    result.update(
        schema_version=3,
        runtime="compact-worker-browser-v1",
        worker_binding=worker_binding,
        capture=mode,
    )
    return result


def validate_receipt(
    value,
    expected_revision,
    *,
    alternate_revision=None,
    expected_tree=None,
    artifact_root=Path("/screenshots"),
    storage_root=None,
    expected_worker=None,
):
    worker = isinstance(value, dict) and value.get("schema_version") == 3
    fields = RECEIPT_FIELDS | {"worker_binding"} if worker else RECEIPT_FIELDS
    if worker and "capture" in value:
        fields |= {"capture"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(
            "Use complete Docker browser receipts; host screenshots cannot qualify this release."
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != (
        3 if worker else 2
    ):
        raise ValueError("Use source-bound Docker browser receipts.")
    validate_binding(
        {**{key: value[key] for key in BINDING_FIELDS}, "schema_version": 1}
    )
    runtime = "compact-worker-browser-v1" if worker else "docker-browser-v1"
    if value["runtime"] != runtime or value["browser"] != "playwright-chromium":
        raise ValueError(
            "Screenshot does not identify the Docker browser implementation."
        )
    if worker:
        validate_worker_binding(value["worker_binding"])
        if value["application_image"] != value["browser_image"]:
            raise ValueError(
                "Worker app and browser must use the same immutable image."
            )
    if expected_worker is not None and (
        not worker or value["worker_binding"] != expected_worker
    ):
        raise ValueError(
            "Browser evidence belongs to a different worker operation or boot."
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
        or target.hostname != ("127.0.0.1" if worker else "vibearr")
        or target.port != (8765 if worker else 8000)
        or target.username
        or target.password
        or target.fragment
    ):
        raise ValueError("Screenshot must render the selected Docker application.")
    if worker:
        validate_page_path(target.path)
        if target.query != "theme=" + value["theme"]:
            raise ValueError("Browser page theme differs from its receipt.")
        mode = validate_capture_mode(
            value.get("capture", {"focus": "page", "full_page": True})
        )
        if mode["focus"] == "factory" and not target.path.startswith("/projects/"):
            raise ValueError(
                "Factory viewport evidence requires the registered project page."
            )
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
    root = Path(storage_root) if storage_root is not None else artifact_root
    path = root / relative
    if root.is_symlink() or path.parent.is_symlink():
        raise ValueError("Screenshot directories cannot be links.")
    content = png_bytes(path)
    width, height = struct.unpack(">II", content[16:24])
    if worker and (
        width != value["viewport"]["width"] or height < value["viewport"]["height"]
    ):
        raise ValueError("Browser PNG dimensions differ from its viewport receipt.")
    if worker and not mode["full_page"] and height != value["viewport"]["height"]:
        raise ValueError("Browser viewport capture includes undeclared page extent.")
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
    storage_root=None,
    expected_worker=None,
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
            storage_root=storage_root,
            expected_worker=expected_worker,
        )
        if any(frame[key] != first[key] for key in BINDING_FIELDS):
            raise ValueError("Browser frames must share one source/image binding.")
        if Path(frame["path"]).parent != Path(first["path"]).parent:
            raise ValueError("Browser frames must belong to one capture report.")
        if (
            expected_worker is not None
            and urlsplit(frame["url"]).path != urlsplit(first["url"]).path
        ):
            raise ValueError("Browser frames must render the same selected page.")
        if expected_worker is not None and frame.get("capture", {}).get(
            "focus", "page"
        ) != first.get("capture", {}).get("focus", "page"):
            raise ValueError("Browser frames must share one selected capture focus.")
        name = "desktop" if frame["viewport"] == VIEWPORTS["desktop"] else "mobile"
        observed.add((name, frame["theme"]))
    if observed != expected:
        raise ValueError("Browser capture coverage is incomplete or duplicated.")
    return frames


def validate_worker_report(report, *, binding, worker_binding, storage_root):
    fields = {
        "schema_version",
        "kind",
        "browser",
        "source_binding",
        "worker_binding",
        "screenshots",
        "page_errors",
        "failed_requests",
        "boundary_checks",
    }
    if not isinstance(report, dict) or set(report) != fields:
        raise ValueError("Supply one complete worker browser report.")
    if type(report["schema_version"]) is not int or report["schema_version"] != 3:
        raise ValueError("Use versioned worker browser capture evidence.")
    if (
        report["kind"] != "compact-worker-browser-acceptance"
        or report["browser"] != "chromium"
    ):
        raise ValueError("Use the registered worker Chromium capture recipe.")
    validate_binding(binding)
    validate_worker_binding(worker_binding)
    if (
        report["source_binding"] != binding
        or report["worker_binding"] != worker_binding
    ):
        raise ValueError(
            "Browser report differs from its expected source or worker operation."
        )
    if report["page_errors"] != [] or report["failed_requests"] != []:
        raise ValueError("Browser errors or failed requests prevent report acceptance.")
    checks = report["boundary_checks"]
    if (
        not isinstance(checks, dict)
        or set(checks)
        != {"control_paths_denied", "credentials_scoped", "source_write_denied"}
        or any(value is not True for value in checks.values())
    ):
        raise ValueError("Browser source and control boundaries were not measured.")
    frames = validate_capture_receipts(
        report["screenshots"],
        binding["revision"],
        expected_tree=binding["tree_sha"],
        expected_worker=worker_binding,
        storage_root=storage_root,
    )
    for frame in frames:
        if any(
            frame[key] != binding[key] for key in BINDING_FIELDS - {"schema_version"}
        ):
            raise ValueError(
                "Browser frame differs from its expected source/image binding."
            )
    return frames


def import_worker_capture(report, archive, *, binding, worker_binding, artifact_root):
    import json
    import shutil
    from uuid import uuid4
    from vibearr_executor.artifacts import extract, unpack

    if not isinstance(archive, bytes) or len(archive) > 16 * 1024**2:
        raise ValueError("Worker browser archive exceeds its transfer limit.")
    files = unpack(archive)
    names = {name.split("/")[0] for name in files}
    if len(names) != 1 or not re.fullmatch(r"capture-[a-f0-9]{32}", next(iter(names))):
        raise ValueError("Import exactly one complete worker browser capture.")
    name = next(iter(names))
    expected = {name + "/browser.json"} | {
        f"{name}/queue-{view}-{theme}.png"
        for view in VIEWPORTS
        for theme in ("light", "dark")
    }
    if set(files) != expected or len(files[name + "/browser.json"][1]) > 1024**2:
        raise ValueError("Worker browser archive has missing or unexpected files.")
    if json.loads(files[name + "/browser.json"][1]) != report:
        raise ValueError("Worker browser archive differs from its exported report.")
    artifact_root = Path(artifact_root)
    if artifact_root.is_symlink():
        raise ValueError("Browser import root cannot be a link.")
    artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = artifact_root / (".import-" + uuid4().hex)
    temporary.mkdir(mode=0o700)
    try:
        extract(archive, temporary)
        frames = validate_worker_report(
            report,
            binding=binding,
            worker_binding=worker_binding,
            storage_root=temporary,
        )
        target = artifact_root / name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or {path.name for path in target.iterdir()} != {
                Path(n).name for n in expected
            }:
                raise ValueError(
                    "A different browser capture already occupies the immutable path."
                )
            for path, (_, content) in files.items():
                actual = artifact_root / path
                if actual.is_symlink() or actual.read_bytes() != content:
                    raise ValueError("The existing browser capture is immutable.")
        else:
            (temporary / name).rename(target)
        for path in target.iterdir():
            path.chmod(0o600)
        target.chmod(0o700)
        validate_worker_report(
            report,
            binding=binding,
            worker_binding=worker_binding,
            storage_root=artifact_root,
        )
        return frames
    finally:
        shutil.rmtree(temporary)


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
    schema = report.get("schema_version")
    expected_kind = (
        "compact-worker-browser-acceptance"
        if schema == 3
        else "docker-browser-acceptance"
    )
    if (
        type(schema) is not int
        or schema not in {2, 3}
        or report.get("kind") != expected_kind
    ):
        raise ValueError(
            "Unbound browser smoke output cannot qualify native acceptance."
        )
    if report.get("page_errors") != [] or report.get("failed_requests") != []:
        raise ValueError("Browser errors prevent acceptance.")
    binding = validate_binding(report["source_binding"])
    worker_binding = None
    if schema == 3:
        worker_binding = validate_worker_binding(report["worker_binding"])
        if worker_binding["run_id"] != run_id:
            raise ValueError("Browser report belongs to a different accepted run.")
    frames = validate_capture_receipts(
        report["screenshots"],
        binding["revision"],
        expected_tree=binding["tree_sha"],
        artifact_root=artifact_root,
        expected_worker=worker_binding,
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
