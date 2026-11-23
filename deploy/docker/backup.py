"""Explicit PostgreSQL 17 backup and empty-target restoration jobs."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID, uuid4

from .backup_files import (
    COMPACT_VOLUMES,
    VOLUMES,
    capture,
    checksum,
    inventory,
    read_json,
    require_capacity,
    restore,
)


ROOT = Path("/var/lib/vibearr-backups")
RUNTIME = "compact-worker-v1"


def backup_volumes(manifest):
    if manifest.get("schema_version") == 2:
        if manifest.get("runtime") != "integrated-runner-v1":
            raise ValueError("Legacy backup runtime is invalid.")
        return VOLUMES
    if (
        manifest.get("schema_version") != 3
        or manifest.get("runtime") != RUNTIME
        or manifest.get("state_volumes") != list(COMPACT_VOLUMES)
    ):
        raise ValueError("Compact backup state-volume mapping is invalid.")
    return COMPACT_VOLUMES


def write_json(path, value):
    with path.open("x") as stream:
        size = 0
        for content in json.JSONEncoder(sort_keys=True, indent=2).iterencode(value):
            size += len(content.encode())
            if size > 64 * 1024**2:
                raise ValueError("Backup metadata exceeds its fixed byte limit.")
            stream.write(content)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def connection(root):
    password = (root / "database-secrets/password").read_text().strip()
    if not password or any(name.startswith("DOCKER_") for name in os.environ):
        raise ValueError(
            "Backup requires declared database credentials and no engine access."
        )
    os.environ.update(
        PGHOST="postgres",
        PGPORT="5432",
        PGUSER=os.environ.get("VIBEARR_POSTGRES_USER", "nightly_queue"),
        PGDATABASE=os.environ.get("VIBEARR_POSTGRES_DB", "nightly_queue"),
        PGPASSWORD=password,
        PGCONNECT_TIMEOUT="10",
        PGOPTIONS="-c timezone=UTC -c extra_float_digits=3 -c statement_timeout=3600000",
    )
    version = int(sql("SHOW server_version_num"))
    if not 170000 <= version < 180000:
        raise ValueError("This backup recipe requires PostgreSQL 17.")


def sql(statement):
    return subprocess.check_output(
        ["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", statement],
        text=True,
        timeout=60,
    ).strip()


def fingerprint():
    tables = json.loads(
        sql(
            "SELECT coalesce(json_agg(json_build_object('schema', schemaname, 'table', tablename) "
            "ORDER BY schemaname, tablename), '[]') FROM pg_tables "
            "WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
        )
    )
    result = []
    for entry in tables:
        names = [entry[key].replace('"', '""') for key in ("schema", "table")]
        relation = '"' + '"."'.join(names) + '"'
        query = f"COPY (SELECT row_to_json(t)::text FROM {relation} AS t ORDER BY row_to_json(t)::text) TO STDOUT"
        process = subprocess.Popen(
            ["psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-c", query],
            stdout=subprocess.PIPE,
        )
        value = hashlib.sha256()
        assert process.stdout is not None
        try:
            while content := process.stdout.read(1024**2):
                value.update(content)
            if process.wait(timeout=3600):
                raise ValueError("Database fingerprint failed.")
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
                process.wait()
        result.append({**entry, "sha256": value.hexdigest()})
    return result


def require_idle():
    exists = sql("SELECT to_regclass('public.runner_control_run') IS NOT NULL")
    if (
        exists == "t"
        and sql("SELECT count(*) FROM runner_control_run WHERE state = 'active'") != "0"
    ):
        raise ValueError(
            "Drain and reconcile active runs before backup; do not discard their state."
        )


def backup(quiesced):
    if not quiesced:
        raise ValueError("Stop state writers and explicitly supply --quiesced.")
    source = Path("/snapshot")
    if (source / "runner-secrets/restore-required.json").exists():
        raise ValueError(
            "Finish explicit restored-identity reconciliation before making another backup."
        )
    connection(source)
    require_idle()
    records = inventory(source, COMPACT_VOLUMES)
    state_size = sum(value["bytes"] for value in records) + len(records) * 8192
    database_size = int(sql("SELECT pg_database_size(current_database())"))
    required = state_size + database_size * 2 + 256 * 1024**2
    if shutil.disk_usage(ROOT).free < required + 1024**3:
        raise ValueError(
            "Backup storage cannot fit this snapshot and a 1 GiB reserve; preserve existing backups."
        )
    identity = str(uuid4())
    directory = ROOT / (".incomplete-" + identity)
    directory.mkdir(mode=0o700)
    before = fingerprint()
    database = directory / "database.dump"
    subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(database),
        ],
        check=True,
        timeout=3600,
    )
    records = capture(source, directory / "state.tar", COMPACT_VOLUMES)
    write_json(directory / "inventory.json", records)
    if before != fingerprint():
        raise ValueError(
            "Database changed during backup; stop all state writers first."
        )
    manifest = {
        "schema_version": 3,
        "runtime": RUNTIME,
        "state_volumes": list(COMPACT_VOLUMES),
        "backup_id": identity,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": {
            "name": os.environ["PGDATABASE"],
            "user": os.environ["PGUSER"],
            "physical_bytes": database_size,
            "tables": before,
        },
        "artifacts": {
            name: {
                "sha256": checksum(directory / name),
                "bytes": (directory / name).stat().st_size,
            }
            for name in ("database.dump", "state.tar", "inventory.json")
        },
    }
    write_json(directory / "manifest.json", manifest)
    for path in directory.iterdir():
        path.chmod(0o600)
        os.chown(path, 10001, 10001)
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    os.chown(directory, 10001, 10001)
    sync_directory(directory)
    directory.rename(ROOT / identity)
    sync_directory(ROOT)
    print(json.dumps({"backup_id": identity, "complete": True, "tables": len(before)}))


def verified_backup(identity):
    if str(UUID(identity)) != identity:
        raise ValueError("Select a canonical backup UUID.")
    directory = Path("/backup-source") / identity
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Select a complete backup directory without links.")
    manifest = read_json(directory / "manifest.json")
    backup_volumes(manifest)
    if manifest.get("backup_id") != identity:
        raise ValueError("Backup manifest does not bind this snapshot.")
    if set(manifest.get("artifacts", {})) != {
        "database.dump",
        "state.tar",
        "inventory.json",
    }:
        raise ValueError("Backup artifact inventory is incomplete.")
    for name, expected in manifest["artifacts"].items():
        path = directory / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != expected["bytes"]
            or checksum(path) != expected["sha256"]
        ):
            raise ValueError("Backup artifact digest or size differs.")
    return directory, manifest


def restore_files(identity):
    directory, manifest = verified_backup(identity)
    volumes = backup_volumes(manifest)
    if volumes != COMPACT_VOLUMES:
        raise ValueError(
            "Restore legacy integrated snapshots with their original tools before an explicit compact migration."
        )
    if any(Path("/restore-database").iterdir()):
        raise ValueError("Restore requires an empty dedicated PostgreSQL volume.")
    records = read_json(directory / "inventory.json")
    required = require_capacity(
        Path("/restore"), records, manifest["database"].get("physical_bytes"), volumes
    )
    if shutil.disk_usage("/restore-database").free < required:
        raise ValueError(
            "PostgreSQL restore storage cannot fit this snapshot and its reserve."
        )
    restore(directory / "state.tar", records, Path("/restore"), volumes)
    marker = Path("/restore/runner-secrets/restore-required.json")
    write_json(marker, {"backup_id": identity, "new_runner_identity_required": True})
    os.chown(marker, 10001, 10001)
    worker_marker = Path("/restore/worker-secrets/restore-required.json")
    write_json(
        worker_marker, {"backup_id": identity, "new_worker_identity_required": True}
    )
    os.chown(worker_marker, 10001, 10001)
    print(
        json.dumps(
            {
                "backup_id": identity,
                "files_restored": True,
                "qualification_required": True,
            }
        )
    )


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_database(identity):
    directory, manifest = verified_backup(identity)
    connection(Path("/restore"))
    if (os.environ["PGDATABASE"], os.environ["PGUSER"]) != (
        manifest["database"]["name"],
        manifest["database"]["user"],
    ):
        raise ValueError("Restore database and user must match the selected backup.")
    if fingerprint():
        raise ValueError("Database restore requires an empty dedicated database.")
    subprocess.run(
        [
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            os.environ["PGDATABASE"],
            str(directory / "database.dump"),
        ],
        check=True,
        timeout=3600,
    )
    if fingerprint() != manifest["database"]["tables"]:
        raise ValueError(
            "Restored database differs from the backup table fingerprints."
        )
    sql(
        "BEGIN; UPDATE runner_control_runneridentity SET enabled = false, controller_digest = ''; "
        "UPDATE runner_control_runnersettings SET enabled = false; "
        "UPDATE runner_control_runnercontroller SET generation = generation + 1; COMMIT"
    )
    fenced = {
        "runner_control_runneridentity",
        "runner_control_runnersettings",
        "runner_control_runnercontroller",
    }
    expected = [
        value
        for value in manifest["database"]["tables"]
        if value["table"] not in fenced
    ]
    observed = [value for value in fingerprint() if value["table"] not in fenced]
    if observed != expected:
        raise ValueError(
            "Restored business data changed while fencing runner authority."
        )
    print(
        json.dumps(
            {
                "backup_id": identity,
                "database_restored": True,
                "table_fingerprints_matched_before_fencing": True,
                "business_data_fingerprints_match": True,
                "restored_runner_identities_disabled": True,
                "processing_disabled": True,
            }
        )
    )


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("backup", "restore-files", "restore-database")
    )
    parser.add_argument("backup_id", nargs="?")
    parser.add_argument("--quiesced", action="store_true")
    options = parser.parse_args()
    if options.action == "backup":
        backup(options.quiesced)
    elif options.backup_id is None:
        parser.error("Restore requires an explicit backup UUID.")
    elif options.action == "restore-files":
        restore_files(options.backup_id)
    else:
        restore_database(options.backup_id)


if __name__ == "__main__":
    main()
