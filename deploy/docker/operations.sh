#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
action=${1:-}
identity=${2:-}
case "$action" in backup|restore-files|restore-database|restore-bootstrap|browser) ;; *) echo 'Choose backup, restore-files UUID, restore-database UUID, restore-bootstrap UUID, or browser.' >&2; exit 2 ;; esac
backup_image=${VIBEARR_BACKUP_IMAGE:-vibearr-backup:local}
export BUILDX_CONFIG=${BUILDX_CONFIG:-/tmp/vibearr-operations-buildx}
docker build --file deploy/docker/Backup.Dockerfile --tag "$backup_image" . >&2
facts_text=$(docker compose config --format json | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python3.12 "$backup_image" -I -c '
import json,sys
config=json.load(sys.stdin)
app=config["services"]["app"]
values=[config["name"], app["environment"]["VIBEARR_POSTGRES_DB"], app["environment"]["VIBEARR_POSTGRES_USER"], config["networks"]["database"]["name"], app["image"], app["environment"]["VIBEARR_RUNNER_NAME"], config["networks"]["app-egress"]["name"]]
if set(config["services"]) != {"app","postgres","runner","worker"}:
    raise ValueError("Compact operations require exactly four runtime services")
values += [config["volumes"][name]["name"] for name in ("application-secrets", "database-secrets", "runner-secrets", "runner-data", "runner-logins", "worker-secrets", "worker-control", "postgres-data")]
for value in values:
    if not isinstance(value,str) or "\n" in value or "\r" in value:
        raise ValueError("Invalid deployment facts")
    print(value)
')
mapfile -t facts <<< "$facts_text"
((${#facts[@]} == 15)) || { echo 'Unable to read the selected compact deployment.' >&2; exit 1; }
project=${facts[0]}
[[ $project =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || exit 2
volumes=(application-secrets database-secrets runner-secrets runner-data runner-logins worker-secrets worker-control)
backup_volume=${VIBEARR_BACKUP_VOLUME:-${project}_backup-data}
source_volume=${VIBEARR_RESTORE_BACKUP_VOLUME:-$backup_volume}
limits=(--rm --read-only --cap-drop ALL --cap-add DAC_OVERRIDE --cap-add CHOWN --cap-add FOWNER --security-opt no-new-privileges:true --user 0:0 --memory 1g --memory-swap 1g --pids-limit 64 --cpus 1 --tmpfs "/tmp:rw,nosuid,nodev,size=64m" --tmpfs "/var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=1m")
environment=(-e "VIBEARR_POSTGRES_DB=${facts[1]}" -e "VIBEARR_POSTGRES_USER=${facts[2]}")
case "$action" in
  backup)
    [[ $identity == --quiesced ]] || { echo 'Stop app, runner and worker, then supply backup --quiesced.' >&2; exit 2; }
    running=$(docker compose ps --status running --services)
    while IFS= read -r service; do
      [[ $service == postgres || -z $service ]] || { echo 'Stop all app, runner and worker state writers before backup.' >&2; exit 1; }
    done <<< "$running"
    docker volume create "$backup_volume" >/dev/null
    docker run "${limits[@]}" --network none --volume "$backup_volume:/backups" --entrypoint python3.12 "$backup_image" -I -c 'import os; os.chmod("/backups",0o700)'
    mounts=(--volume "$backup_volume:/var/lib/vibearr-backups")
    for index in "${!volumes[@]}"; do
      mounts+=(--volume "${facts[index+7]}:/snapshot/${volumes[index]}:ro")
    done
    docker run "${limits[@]}" --network "${facts[3]}" "${environment[@]}" "${mounts[@]}" "$backup_image" backup --quiesced
    ;;
  restore-files)
    [[ $identity =~ ^[a-f0-9-]{36}$ ]] || exit 2
    existing=$(docker compose ps -aq)
    [[ -z $existing ]] || { echo 'Restore file targets must be a new deployment without containers.' >&2; exit 1; }
    docker volume inspect "$source_volume" >/dev/null
    mounts=(--volume "$source_volume:/backup-source:ro")
    for index in "${!volumes[@]}"; do
      docker volume create --label "com.docker.compose.project=$project" --label "com.docker.compose.volume=${volumes[index]}" "${facts[index+7]}" >/dev/null
      mounts+=(--volume "${facts[index+7]}:/restore/${volumes[index]}")
    done
    docker volume create --label "com.docker.compose.project=$project" --label com.docker.compose.volume=postgres-data "${facts[14]}" >/dev/null
    mounts+=(--volume "${facts[14]}:/restore-database")
    docker run "${limits[@]}" --network none "${mounts[@]}" "$backup_image" restore-files "$identity"
    ;;
  restore-database)
    [[ $identity =~ ^[a-f0-9-]{36}$ ]] || exit 2
    docker compose up -d --wait postgres >&2
    docker run "${limits[@]}" --network "${facts[3]}" "${environment[@]}" --volume "$source_volume:/backup-source:ro" --volume "${facts[8]}:/restore/database-secrets:ro" "$backup_image" restore-database "$identity"
    ;;
  restore-bootstrap)
    [[ $identity =~ ^[a-f0-9-]{36}$ ]] || exit 2
    existing=$(docker compose ps -q worker)
    [[ -z $existing ]] || { echo 'Retire restored worker authority before starting its container.' >&2; exit 1; }
    docker run "${limits[@]}" --network none --volume "${facts[12]}:/restore/worker-secrets" --volume "${facts[13]}:/restore/worker-control" --entrypoint python3.12 "$backup_image" -I -m deploy.docker.worker_recovery "$identity"
    docker run --rm --network none --read-only --cap-drop ALL --cap-add DAC_OVERRIDE --cap-add CHOWN --security-opt no-new-privileges:true --user 0:0 --memory 128m --pids-limit 16 --cpus 1 --volume "${facts[9]}:/restore/runner-secrets:ro" --volume "${facts[10]}:/restore/runner-data" --entrypoint python3.12 "$backup_image" -I -m deploy.docker.runner_recovery "$identity"
    docker run --rm --network "${facts[3]}" --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 --memory 512m --pids-limit 64 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=64m "${environment[@]}" -e VIBEARR_POSTGRES_HOST=postgres --volume "${facts[7]}:/run/vibearr/application:ro" --volume "${facts[8]}:/run/vibearr/database:ro" --volume "${facts[9]}:/run/vibearr/runner" --volume "${facts[10]}:/var/lib/vibearr" --entrypoint python "${facts[4]}" -m deploy.docker.entrypoint python manage.py docker_bootstrap_runner "${facts[5]}" --restored-backup "$identity"
    ;;
  browser)
    browser_image=${VIBEARR_BROWSER_IMAGE:-vibearr-browser:local}
    artifact_volume=${project}_browser-artifacts
    docker build --file deploy/docker/Browser.Dockerfile --tag "$browser_image" . >&2
    docker volume create "$artifact_volume" >/dev/null
    docker run "${limits[@]}" --network none --volume "$artifact_volume:/artifacts" --entrypoint python3.12 "$backup_image" -I -c 'import os; os.chown("/artifacts",10001,10001); os.chmod("/artifacts",0o700)'
    docker run --rm --network "${facts[6]}" --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 --memory 1g --pids-limit 128 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,exec,size=256m --volume "$artifact_volume:/artifacts" -e "VIBEARR_BROWSER_SOURCE_BINDING=${VIBEARR_BROWSER_SOURCE_BINDING:-}" "$browser_image"
    ;;
esac
