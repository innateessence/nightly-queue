"""Trusted lifetime of candidate app services and private browser evidence."""

from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

from .artifacts import extract, pack, snapshot, unpack
from .contracts import (
    PhaseEvidenceRejected,
    Profile,
    Rejected,
    canonical,
    digest,
    identifier,
)
from .runtime import durable
from .local_app import APP_UID
from vibearr_runner.browser_receipts import (
    validate_binding,
    validate_capture_mode,
    validate_page_path,
    validate_worker_binding,
    validate_worker_report,
)

PROFILE_ID = "browser-ui"
PORT = 8765
MAX_CAPTURE = 16 * 1024**2
ARGV = ("/usr/local/bin/python", "-I", "-m", "vibearr_executor.local_browser_task")


class BrowserEvidenceError(PhaseEvidenceRejected):
    pass


def source_tree(content, object_length):
    algorithm = "sha1" if object_length == 40 else "sha256"

    def object_id(kind, body):
        header = kind + b" " + str(len(body)).encode() + b"\0"
        return hashlib.new(algorithm, header + body).digest()

    root = {}
    for name, (mode, data) in unpack(content).items():
        directory = root
        parts = name.split("/")
        for part in parts[:-1]:
            directory = directory.setdefault(part, {})
        directory[parts[-1]] = (mode, data)

    def tree_id(directory):
        def order(entry):
            name, value = entry
            return (name + ("/" if isinstance(value, dict) else "")).encode()

        entries = []
        for name, value in sorted(directory.items(), key=order):
            if isinstance(value, dict):
                mode, identity = b"40000", tree_id(value)
            else:
                permissions, data = value
                mode = b"100755" if permissions & 0o111 else b"100644"
                identity = object_id(b"blob", data)
            entries.append(mode + b" " + name.encode() + b"\0" + identity)
        return object_id(b"tree", b"".join(entries))

    return tree_id(root).hex()


def read_report(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > 1024**2
        ):
            raise Rejected(
                "Browser report must be a bounded regular file without links."
            )
        content = stream.read(1024**2 + 1)
    if len(content) > 1024**2:
        raise Rejected("Browser report exceeds its byte limit.")
    return json.loads(content)


def browser_profile(image, limits):
    return Profile(
        PROFILE_ID, 1, image, "validation", ARGV, limits, database_image=image
    )


def service_processes():
    result = []
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
            state = next(
                line.split()[1]
                for line in status.splitlines()
                if line.startswith("State:")
            )
            if uid == str(APP_UID) and state != "Z":
                result.append(int(path.name))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def stop_services(processes):
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    expires = time.monotonic() + 10
    while time.monotonic() < expires:
        alive = service_processes()
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for process in processes:
            if process.poll() is None:
                process.wait(timeout=1)
        if not alive:
            return
        time.sleep(0.02)
    raise RuntimeError("Candidate app descendants survived cleanup; fence the worker.")


class LocalServices:
    def __init__(self, database=None, *, root=Path("/browser-services")):
        self.database = database
        self.root = root
        self.runtime = None
        self.active = ""
        self.processes = []
        self.binding = None
        self.worker_binding = None

    def bind(self, runtime):
        self.runtime = runtime
        stop_services([])
        if self.root.is_symlink():
            raise Rejected("Browser service root cannot be a link.")
        if self.root.exists():
            for path in self.root.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()

    def _runtime(self):
        if self.runtime is None:
            raise RuntimeError("Bind local services to the trusted worker runtime.")
        return self.runtime

    def before_phase(self, operation_id, profile, context):
        runtime = self._runtime()
        identifier(operation_id)
        if profile.profile_id != PROFILE_ID:
            reference = context["document"].get("browser_capture")
            if reference is not None:
                self._review_inputs(context, reference)
            return
        if self.active or service_processes():
            raise Rejected("A prior candidate app still owns the browser phase.")
        if (
            profile.argv != ARGV
            or profile.phase != "validation"
            or profile.network_hosts
            or profile.backend_secret_env
        ):
            raise Rejected("Browser execution requires the fixed offline UI profile.")
        manifest = context["manifest"]
        if (
            manifest["operation_id"] != operation_id
            or manifest["profile_digest"] != profile.digest
        ):
            raise Rejected("Browser operation differs from its trusted launch.")
        binding = validate_binding(context["document"].get("browser_binding"))
        if binding["source_digest"] != manifest["source_digest"]:
            raise Rejected("Browser source binding differs from the candidate.")
        if (
            binding["application_image"] != profile.image
            or binding["browser_image"] != profile.image
        ):
            raise Rejected(
                "Browser app and tools must identify the actual worker image."
            )
        content = snapshot(runtime.work)
        if digest(content) != manifest["source_digest"]:
            raise Rejected("Browser workspace differs from its exact source snapshot.")
        if source_tree(content, len(binding["tree_sha"])) != binding["tree_sha"]:
            raise Rejected("Browser Git tree differs from its actual source files.")
        environment = context["environment"]
        if not environment.get("DATABASE_URL") or environment.get(
            "VIBEARR_BACKEND_SECRET_FILE"
        ):
            raise Rejected(
                "Browser app needs a disposable database and no backend secret."
            )
        worker = validate_worker_binding(
            {
                "run_id": manifest["run_id"],
                "operation_id": operation_id,
                "execution_identity": context["execution_identity"],
                "profile_id": PROFILE_ID,
                "profile_digest": profile.digest,
            }
        )
        page_path = validate_page_path(context["document"].get("browser_page", "/"))
        focus = context["document"].get("browser_focus", "page")
        mode = validate_capture_mode({"focus": focus, "full_page": focus == "page"})
        if mode["focus"] == "factory" and not page_path.startswith("/projects/"):
            raise Rejected(
                "Factory viewport capture requires a registered project page."
            )
        self.active, self.binding, self.worker_binding = operation_id, binding, worker
        self.root.mkdir(mode=0o711, parents=True, exist_ok=True)
        self.root.chmod(0o711)
        directory = self.root / operation_id
        directory.mkdir(mode=0o700)
        os.chown(directory, APP_UID, APP_UID)
        settings = {
            "source_binding": binding,
            "worker_binding": worker,
            "page_path": page_path,
            "capture_focus": focus,
        }
        durable(runtime.inputs / "browser.json", canonical(settings), 0o444)
        browser_root = runtime.home / "browser"
        browser_root.mkdir(mode=0o700)
        os.chown(browser_root, 1000, 1000)
        deadline = min(
            datetime.fromisoformat(manifest["deadline"]).timestamp(), time.time() + 120
        )
        durable(
            runtime.path(operation_id) / "browser-window.json",
            canonical(
                {
                    "not_before": time.time(),
                    "deadline": deadline,
                    "page_path": page_path,
                    "capture_focus": focus,
                }
            ),
        )
        app_environment = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(directory),
            "TMPDIR": str(directory),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "LANG": "C.UTF-8",
            "VIBEARR_DISPOSABLE_DATABASE_URL": environment["DATABASE_URL"],
            "VIBEARR_BROWSER_APP_ROOT": str(directory),
        }
        try:
            prepare = self._spawn("prepare", app_environment, directory)
            self._wait(prepare, deadline)
            app = self._spawn("serve", app_environment, directory)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.time() < deadline and not runtime.cancel.is_set():
                if app.poll() is not None:
                    raise RuntimeError(
                        "Candidate browser application exited before readiness."
                    )
                try:
                    with opener.open(
                        f"http://127.0.0.1:{PORT}/health/", timeout=0.5
                    ) as response:
                        if response.status == 200:
                            return
                except (OSError, urllib.error.URLError):
                    time.sleep(0.05)
            raise TimeoutError(
                "Candidate browser app did not become ready before its deadline."
            )
        except BaseException:
            self.after_phase(operation_id, capture=False)
            raise

    def _spawn(self, mode, environment, directory):
        path = self._runtime().path(self.active) / f"app-{mode}-{uuid4().hex}.log"
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "wb") as log:
            process = subprocess.Popen(
                [
                    "/usr/local/bin/python",
                    "-I",
                    "-m",
                    "vibearr_executor.local_app",
                    mode,
                ],
                cwd="/",
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.processes.append(process)
        return process

    def _wait(self, process, deadline):
        runtime = self._runtime()
        while process.poll() is None:
            if runtime.cancel.is_set() or time.time() >= deadline:
                raise TimeoutError(
                    "Candidate browser application setup was canceled or timed out."
                )
            time.sleep(0.05)
        if process.returncode:
            raise RuntimeError("Candidate browser application setup failed.")

    def after_phase(self, operation_id, *, capture=True):
        if not self.active:
            return
        if operation_id != self.active:
            raise Rejected("Browser cleanup cannot stop a different operation.")
        runtime = self._runtime()
        stop_services(self.processes)
        try:
            if capture and not runtime.cancel.is_set():
                self._preserve(operation_id)
        except (KeyError, OSError, TypeError, ValueError, Rejected) as error:
            raise BrowserEvidenceError(
                "Worker browser evidence is missing or invalid."
            ) from error
        finally:
            shutil.rmtree(self.root / operation_id)
            self.processes = []
            self.active = ""
            self.binding = self.worker_binding = None

    def _preserve(self, operation_id):
        runtime = self._runtime()
        root = runtime.home / "browser"
        reports = list(root.glob("capture-*/browser.json"))
        if len(reports) != 1:
            raise Rejected("Browser phase must produce exactly one complete capture.")
        if reports[0].parent.is_symlink():
            raise Rejected("Browser capture directory cannot be a link.")
        report = read_report(reports[0])
        if (
            report.get("source_binding") != self.binding
            or report.get("worker_binding") != self.worker_binding
        ):
            raise Rejected("Browser report differs from the trusted operation binding.")
        self._validate_report(report, root)
        self._validate_window(operation_id, report)
        original = runtime.record(operation_id)["manifest"]["source_digest"]
        if digest(snapshot(runtime.work)) != original:
            raise Rejected("Candidate source changed during browser rendering.")
        target = runtime.path(operation_id) / "browser"
        if target.exists():
            raise Rejected("Browser operation already has immutable capture evidence.")
        target.mkdir(mode=0o700)
        capture = reports[0].parent
        expected = {"browser.json"} | {
            Path(frame["path"]).name for frame in report["screenshots"]
        }
        if {path.name for path in capture.iterdir()} != expected:
            raise Rejected("Browser capture contains unexpected files.")
        shutil.copytree(capture, target / capture.name, symlinks=False)
        for path in (target / capture.name).iterdir():
            os.chown(path, 0, 0)
            path.chmod(0o600)
        os.chown(target / capture.name, 0, 0)
        (target / capture.name).chmod(0o700)
        self._validate_report(report, target)

    @staticmethod
    def _validate_report(report, root):
        binding = validate_binding(report["source_binding"])
        worker = validate_worker_binding(report["worker_binding"])
        validate_worker_report(
            report, binding=binding, worker_binding=worker, storage_root=root
        )

    def export(self, operation_id):
        runtime = self._runtime()
        root = runtime.path(identifier(operation_id)) / "browser"
        reports = list(root.glob("capture-*/browser.json"))
        if len(reports) != 1:
            raise Rejected("No complete browser evidence exists for this operation.")
        if reports[0].parent.is_symlink():
            raise Rejected("Browser capture directory cannot be a link.")
        report = read_report(reports[0])
        self._validate_report(report, root)
        self._validate_window(operation_id, report)
        record = runtime.record(operation_id)
        manifest = record["manifest"]
        if (
            report["worker_binding"]["execution_identity"]
            != record["execution_identity"]
            or report["worker_binding"]["operation_id"] != operation_id
            or report["worker_binding"]["run_id"] != manifest["run_id"]
            or report["worker_binding"]["profile_digest"] != manifest["profile_digest"]
            or report["source_binding"]["source_digest"] != manifest["source_digest"]
        ):
            raise Rejected(
                "Browser evidence differs from its original worker manifest."
            )
        paths = list(reports[0].parent.iterdir())
        expected = {"browser.json"} | {
            Path(frame["path"]).name for frame in report["screenshots"]
        }
        if {path.name for path in paths} != expected:
            raise Rejected("Browser evidence has unexpected files.")
        if sum(path.stat().st_size for path in paths) > MAX_CAPTURE - 65536:
            raise Rejected("Browser evidence exceeds its transfer limit.")
        files = {
            str(path.relative_to(root)): (0o644, path.read_bytes()) for path in paths
        }
        archive = pack(files)
        if len(archive) > MAX_CAPTURE:
            raise Rejected("Browser evidence exceeds its transfer limit.")
        return report, archive

    def _validate_window(self, operation_id, report):
        window = read_report(self._runtime().path(operation_id) / "browser-window.json")
        for frame in report["screenshots"]:
            captured = datetime.fromisoformat(frame["captured_at"]).timestamp()
            if not window["not_before"] <= captured <= window["deadline"]:
                raise Rejected("Browser evidence was captured outside this operation.")
            if (
                frame["url"].split("?", 1)[0]
                != f"http://127.0.0.1:{PORT}" + window["page_path"]
            ):
                raise Rejected(
                    "Browser evidence rendered a different application page."
                )
            if frame.get("capture", {}).get("focus", "page") != window.get(
                "capture_focus", "page"
            ):
                raise Rejected(
                    "Browser evidence differs from its selected capture focus."
                )

    def qualify_browser(self):
        from .browser_fixture import source

        runtime = self._runtime()
        profile = runtime.profiles.get(PROFILE_ID)
        if profile is None:
            raise Rejected(
                "Install the registered browser profile before qualification."
            )
        content, identities = source()
        binding = {
            "schema_version": 1,
            **identities,
            "application_image": profile.image,
            "browser_image": profile.image,
        }
        operation = str(uuid4())
        context = canonical({"browser_binding": binding})
        runtime.probe_operation, runtime.probe_profile = operation, profile
        for kind, value in (("source", content), ("context", context)):
            runtime.upload(kind, value, digest(value))
        manifest = {
            "schema_version": 1,
            "run_id": str(uuid4()),
            "operation_id": operation,
            "profile_id": PROFILE_ID,
            "profile_digest": profile.digest,
            "source_digest": digest(content),
            "context_digest": digest(context),
            "phase": "validation",
            "limits": profile.data()["limits"],
            "network_hosts": [],
            "deadline": (
                datetime.now(timezone.utc) + timedelta(seconds=120)
            ).isoformat(),
        }
        try:
            runtime.prepare(manifest)
            runtime.start(operation)
            runtime.thread.join(timeout=130)
            record = runtime.record(operation)
            entry = self._qualification_entry(record, runtime.execution_identity())
            return {"operation_id": operation, "entry": entry}
        finally:
            if runtime.active == operation:
                runtime.stop(operation)

    def _qualification_entry(self, record, identity):
        from .compact_role_qualification import role_entry

        result = record.get("result", {})
        if (
            record.get("state") != "exited"
            or record.get("descendants_terminated") is not True
            or result.get("exit_code") != 0
            or result.get("terminated") is not True
            or result.get("timed_out") is not False
            or result.get("canceled") is not False
            or result.get("export_error")
            or record.get("execution_identity") != identity
        ):
            raise Rejected(
                "Browser qualification requires a successful current-boot phase."
            )
        report, archive = self.export(record["operation_id"])
        checks = {
            **report["boundary_checks"],
            "browser_rendered": True,
            "descendants_terminated": True,
        }
        runtime = self._runtime()
        entry = role_entry(
            runtime, "browser", runtime.profiles[PROFILE_ID], record, checks
        )
        proof = entry["qualification"]
        proof.pop("proof_digest")
        proof["browser_archive_digest"] = digest(archive)
        proof["proof_digest"] = digest(
            canonical({"proof": proof, "receipt_digest": record["receipt_digest"]})
        )
        return entry

    def qualified_browser(self, saved, identity):
        try:
            record = self._runtime().record(identifier(saved["operation_id"]))
            entry = self._qualification_entry(record, identity)
            return entry if saved.get("entry") == entry else None
        except (KeyError, OSError, ValueError, Rejected):
            return None

    def _review_inputs(self, context, reference):
        if not isinstance(reference, dict) or set(reference) != {
            "operation_id",
            "archive_digest",
        }:
            raise Rejected(
                "Select one exact prior browser operation for visual review."
            )
        runtime = self._runtime()
        record = runtime.record(identifier(reference["operation_id"]))
        result = record.get("result", {})
        if (
            record.get("state") != "exited"
            or record.get("descendants_terminated") is not True
            or result.get("terminated") is not True
            or result.get("timed_out") is not False
            or result.get("canceled") is not False
            or result.get("export_error")
        ):
            raise Rejected(
                "Visual review requires a completed, terminated browser operation."
            )
        if (
            record.get("result", {}).get("exit_code") != 0
            or record["manifest"]["profile_id"] != PROFILE_ID
        ):
            raise Rejected(
                "Visual review requires a successful registered browser operation."
            )
        report, archive = self.export(reference["operation_id"])
        binding = report["source_binding"]
        worker = report["worker_binding"]
        if digest(archive) != reference["archive_digest"]:
            raise Rejected("Visual review browser archive digest changed.")
        if (
            binding["source_digest"] != context["manifest"]["source_digest"]
            or worker["run_id"] != context["manifest"]["run_id"]
        ):
            raise Rejected(
                "Visual review browser evidence belongs to a different candidate or run."
            )
        if worker["execution_identity"] != context["execution_identity"]:
            raise Rejected(
                "Recapture UI evidence after the worker identity or boot changes."
            )
        target = runtime.inputs / "browser"
        extract(archive, target)
        for parent, directories, files in os.walk(target):
            Path(parent).chmod(0o555)
            for name in directories:
                (Path(parent) / name).chmod(0o555)
            for name in files:
                (Path(parent) / name).chmod(0o444)
