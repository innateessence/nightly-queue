"""Model-free permission probes bound to fixed role recipes and this worker boot."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time
from uuid import uuid4

from .artifacts import pack
from .compact_profiles import ROLES, RUNTIME, source_access
from .contracts import Conflict, Rejected, canonical, digest
from .runtime import durable


PROBE = """import json,os,pathlib,subprocess
context=json.loads(pathlib.Path(os.environ['VIBEARR_CONTEXT']).read_text())
role=os.environ['VIBEARR_FACTORY_ROLE']
assert os.getuid()==1000
assert 'CapEff:\\t0000000000000000' in pathlib.Path('/proc/self/status').read_text()
source=pathlib.Path('/work/probe.txt')
write=False
try:
    with source.open('r+b') as stream: stream.write(b'changed')
    write=True
except PermissionError: pass
create=False
try:
    pathlib.Path('/work/new-file').write_text('candidate output')
    create=True
except PermissionError: pass
chmod=False
try:
    source.chmod(0o644)
    chmod=True
except PermissionError: pass
denied=False
try: pathlib.Path(context['private_path']).read_bytes()
except PermissionError: denied=True
assert denied
checks={'control_paths_denied':denied,'credentials_scoped':not pathlib.Path('/secrets/backend').exists()}
if role=='build': checks['candidate_write_only']=write and create
else: checks['source_write_denied']=not(write or create or chmod) and source.stat().st_uid!=1000
assert all(checks.values())
subprocess.Popen(['/usr/local/bin/python','-I','-c','import time;time.sleep(120)'],start_new_session=True)
print('FACTORY_PROBE:'+json.dumps(checks),flush=True)
"""


def probe_profile(profile):
    return replace(
        profile,
        argv=("/usr/local/bin/python", "-I", "-c", PROBE),
        network_hosts=(),
        backend_secret_env="",
        database_image="",
    )


def measured_checks(runtime, record):
    result = record.get("result", {})
    if (
        record.get("state") != "exited"
        or record.get("descendants_terminated") is not True
        or result.get("exit_code") != 0
        or result.get("execution_identity") != runtime.execution_identity()
        or result.get("export_error")
    ):
        raise Rejected("Factory permission probe did not terminate successfully.")
    lines = runtime.logs(record["operation_id"], 0, 65536).decode().splitlines()
    observations = [
        json.loads(line[14:]) for line in lines if line.startswith("FACTORY_PROBE:")
    ]
    if len(observations) != 1 or not isinstance(observations[0], dict):
        raise Rejected("Factory permission observation is missing or ambiguous.")
    checks = observations[0]
    if not checks or any(value is not True for value in checks.values()):
        raise Rejected("Factory permission observation failed.")
    checks["descendants_terminated"] = True
    return checks


def role_entry(runtime, role, profile, record, checks):
    identity = runtime.execution_identity()
    proof = {
        "schema_version": 1,
        "kind": "worker-boundary",
        "role": role,
        "source_access": source_access(profile.profile_id),
        "profile_digest": profile.digest,
        "image_digest": profile.image,
        "runtime_binding_digest": identity["binding_digest"],
        "instance_id": runtime.instance,
        "worker_boot": runtime.boot,
        "passed": True,
        "checks": checks,
    }
    proof["proof_digest"] = digest(
        canonical({"proof": proof, "receipt_digest": record["receipt_digest"]})
    )
    return {
        "profile_id": profile.profile_id,
        "profile_digest": profile.digest,
        "source_access": proof["source_access"],
        "qualification": proof,
    }


def qualified_roles(runtime, identity):
    path = runtime.directory / "compact-role-qualification.json"
    report = json.loads(path.read_bytes()) if path.exists() else {}
    if report.get("execution_identity") != identity:
        return {}
    installed = {}
    for role in ROLES:
        saved = report.get("roles", {}).get(role)
        profile = runtime.profiles.get("factory-" + role)
        if not isinstance(saved, dict) or profile is None:
            continue
        try:
            record = runtime.record(saved.get("operation_id", ""))
            if (
                record.get("manifest", {}).get("profile_digest")
                != probe_profile(profile).digest
            ):
                continue
            checks = measured_checks(runtime, record)
            entry = role_entry(runtime, role, profile, record, checks)
        except (Rejected, OSError, ValueError):
            continue
        if saved.get("entry") == entry:
            installed[role] = entry
    browser = report.get("browser")
    if isinstance(browser, dict) and hasattr(runtime.services, "qualified_browser"):
        observed = runtime.services.qualified_browser(browser, identity)
        if observed is not None:
            installed["browser"] = observed
    return installed


def qualify_roles(runtime):
    if not runtime.qualification_lock.acquire(blocking=False):
        raise Conflict("Another qualification owns the worker.")
    try:
        if (
            runtime.active
            or runtime.fenced
            or runtime.capabilities().get("qualified") is not True
        ):
            raise Conflict("Qualify an idle unfenced worker boot first.")
        runtime.qualifying = True
        sentinel = runtime.directory / "role-private-sentinel"
        durable(sentinel, b"Protected qualification-only sentinel.")
        roles = {}
        for role in ROLES:
            profile = runtime.profiles["factory-" + role]
            probe = probe_profile(profile)
            operation = str(uuid4())
            runtime.probe_operation, runtime.probe_profile = operation, probe
            source = pack({"probe.txt": (0o644, b"candidate input\n")})
            context = canonical({"qualification": True, "private_path": str(sentinel)})
            runtime.upload("source", source, digest(source))
            runtime.upload("context", context, digest(context))
            manifest = {
                "schema_version": 1,
                "run_id": str(uuid4()),
                "operation_id": operation,
                "profile_id": probe.profile_id,
                "profile_digest": probe.digest,
                "source_digest": digest(source),
                "context_digest": digest(context),
                "phase": "agent",
                "limits": probe.data()["limits"],
                "deadline": (
                    datetime.now(timezone.utc) + timedelta(seconds=30)
                ).isoformat(),
                "network_hosts": [],
            }
            runtime.prepare(manifest)
            runtime.start(operation)
            deadline = time.monotonic() + 40
            while (
                runtime.status(operation)["state"] == "running"
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if runtime.thread is not None:
                runtime.thread.join(timeout=5)
            record = runtime.record(operation)
            checks = measured_checks(runtime, record)
            entry = role_entry(runtime, role, profile, record, checks)
            roles[role] = {"operation_id": operation, "entry": entry}
        report = {
            "schema_version": 1,
            "runtime": RUNTIME,
            "execution_identity": runtime.execution_identity(),
            "roles": roles,
        }
        if runtime.services is not None and hasattr(
            runtime.services, "qualify_browser"
        ):
            report["browser"] = runtime.services.qualify_browser()
        durable(
            runtime.directory / "compact-role-qualification.json", canonical(report)
        )
        return report
    finally:
        if runtime.active and runtime.active == runtime.probe_operation:
            runtime.stop(runtime.active)
        runtime.qualifying = False
        runtime.probe_operation, runtime.probe_profile = "", None
        runtime.qualification_lock.release()
