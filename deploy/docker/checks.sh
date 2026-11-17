#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
recipe=$PWD
mode=${1:-full}
if (($#)); then shift; fi
candidate=''
commit=''
base=''
introduction=''
while (($#)); do
  case "$1" in
    --candidate) candidate=$2; shift 2 ;;
    --commit) commit=$2; shift 2 ;;
    --base) base=$2; shift 2 ;;
    --introduction) introduction=$2; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
case "$mode" in full|unit|boundary|candidate|operations) ;; *) echo 'Choose full, unit, boundary, candidate, or operations.' >&2; exit 2 ;; esac
if [[ -n $candidate && -z $commit ]]; then echo 'Exact candidate input requires --commit.' >&2; exit 2; fi
if [[ -n $candidate && $mode == full ]]; then echo 'Run candidate, boundary and operations separately for exact input.' >&2; exit 2; fi
project=${VIBEARR_CHECK_PROJECT:-vibearr-check-${GITHUB_RUN_ID:-local}-${RANDOM}-${RANDOM}}
if [[ ! $project =~ ^vibearr-check-[a-z0-9-]+$ || ${#project} -gt 50 ]]; then echo 'Use a disposable vibearr-check-* project of at most 50 characters.' >&2; exit 2; fi
reservation=/tmp/${project}.check-lock
mkdir -m 700 "$reservation"
trap 'rmdir "$reservation"' EXIT
for selected_project in "$project" "${project}-restore"; do
  if [[ -n $(docker ps -aq --filter "label=com.docker.compose.project=$selected_project") || -n $(docker volume ls -q --filter "name=^${selected_project}_") ]]; then
    echo 'The selected check project already has state; select a new name.' >&2
    exit 2
  fi
done
scratch=$(mktemp -d /tmp/vibearr-check.XXXXXXXX)
output=${VIBEARR_CHECK_OUTPUT:-/tmp/$project-reports}
mkdir "$output"
export BUILDX_CONFIG=${BUILDX_CONFIG:-$scratch/buildx}
unset VIBEARR_API_TOKEN NQ_API_TOKEN VIBEARR_POSTGRES_PASSWORD POSTGRES_PASSWORD
unset VIBEARR_CREDENTIAL_KEY NQ_CREDENTIAL_KEY DJANGO_SECRET_KEY VIBEARR_RUNNER_API_TOKEN NQ_RUNNER_API_TOKEN
unset COMPOSE_PROFILES COMPOSE_ENV_FILES
unset VIBEARR_AGENT_IMAGE_ID VIBEARR_WORKER_IMAGE_ID VIBEARR_DATABASE_IMAGE_ID
export COMPOSE_DISABLE_ENV_FILE=1
export PORT=0 VIBEARR_BIND=127.0.0.1 DJANGO_ALLOWED_HOSTS='*'
export VIBEARR_DATA_PROJECT=$project VIBEARR_BACKUP_VOLUME=${project}_backup-data
export VIBEARR_POSTGRES_DB=vibearr_checks VIBEARR_POSTGRES_USER=vibearr_checks
export VIBEARR_APP_IMAGE=$project-app:checks VIBEARR_TOOLS_IMAGE=$project-tools:checks
export VIBEARR_WORKER_IMAGE=$project-worker:checks VIBEARR_AGENT_IMAGE=$project-agent:checks
export VIBEARR_RUNNER_IMAGE=$project-runner:checks VIBEARR_BACKUP_IMAGE=$project-backup:checks
export VIBEARR_BROWSER_IMAGE=$project-browser:checks VIBEARR_RESTORE_BACKUP_VOLUME=${project}_backup-data
restore_project=${project}-restore
printf 'PORT=0\n' > "$scratch/compose.env"

compose() {
  docker compose --env-file "$scratch/compose.env" --file "$recipe/docker-compose.yml" --project-name "$project" --profile runner --profile checks --profile tools --profile operations "$@"
}
restored() {
  VIBEARR_DATA_PROJECT=$restore_project VIBEARR_BACKUP_VOLUME=${restore_project}_backup-data \
    docker compose --env-file "$scratch/compose.env" --file "$recipe/docker-compose.yml" --project-name "$restore_project" --profile operations --profile tools "$@"
}
export_volume() {
  local volume=$1 destination=$2
  if ! docker volume inspect "$volume" >/dev/null 2>&1; then return; fi
  mkdir -p "$destination"
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --volume "$volume:/data:ro" --entrypoint tar "$VIBEARR_TOOLS_IMAGE" -C /data -cf - . | tar -xf - -C "$destination"
}
cleanup() {
  local result=$?
  trap - EXIT
  compose logs --no-color --tail 100 > "$output/services.log" 2>&1 || true
  export_volume "${project}_check-artifacts" "$output/checks" || result=1
  export_volume "${project}_browser-artifacts" "$output/browser" || result=1
  restored down --volumes --remove-orphans > "$output/restore-cleanup.log" 2>&1 || result=1
  compose down --volumes --remove-orphans > "$output/cleanup.log" 2>&1 || result=1
  rm -rf "$scratch"
  rmdir "$reservation"
  echo "Docker check artifacts: $output"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Building disposable Docker check images for $project"
compose build initialize > "$output/build.log" 2>&1
source_image=$project-source:checks
if [[ -n $candidate ]]; then
  candidate=$(realpath "$candidate")
  git_directory=$candidate/.git
  if [[ -f $git_directory ]]; then
    IFS= read -r reference < "$git_directory"
    [[ $reference == 'gitdir: '* ]] || exit 2
    git_directory=${reference#gitdir: }
    [[ $git_directory == /* ]] || git_directory=$candidate/$git_directory
  fi
  common=$git_directory
  if [[ -f $git_directory/commondir ]]; then
    IFS= read -r reference < "$git_directory/commondir"
    common=$(realpath "$git_directory/$reference")
  fi
  mkdir -p "$scratch/input/git/refs"
  cp -R "$common/objects" "$scratch/input/git/objects"
  cp -R "$common/refs/." "$scratch/input/git/refs/"
  cp "$git_directory/HEAD" "$scratch/input/git/HEAD"
  if [[ -f $common/packed-refs ]]; then cp "$common/packed-refs" "$scratch/input/git/packed-refs"; else : > "$scratch/input/git/packed-refs"; fi
  docker build --file "$recipe/deploy/docker/GitInput.Dockerfile" --tag "$source_image" "$scratch/input" > "$output/source-build.log" 2>&1
else
  docker build --file "$recipe/deploy/docker/WorktreeInput.Dockerfile" --tag "$source_image" "$recipe" > "$output/source-build.log" 2>&1
fi
runtime_source=$recipe
if [[ -n $candidate && ( $mode == boundary || $mode == operations ) ]]; then
  input_image=$project-input:checks
  docker build --file deploy/docker/Candidate.Dockerfile --build-arg "SOURCE_IMAGE=$source_image" --build-arg "TOOLS_IMAGE=$VIBEARR_TOOLS_IMAGE" --tag "$input_image" . > "$output/input-build.log" 2>&1
  arguments=(--commit "$commit")
  if [[ -n $base ]]; then arguments+=(--base "$base"); fi
  if [[ -n $introduction ]]; then arguments+=(--introduction "$introduction"); fi
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --memory 512m --memory-swap 512m --pids-limit 64 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --entrypoint python "$input_image" -m deploy.docker.source_export "${arguments[@]}" > "$scratch/source.tar" 2> "$output/implementation-source.json"
  runtime_source=$scratch/source
  mkdir "$runtime_source"
  tar -xf "$scratch/source.tar" -C "$runtime_source"
fi
docker build --file "$recipe/Dockerfile" --target application --tag "$VIBEARR_APP_IMAGE" "$runtime_source" >> "$output/build.log" 2>&1
compose config --format json | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.audit > "$output/compose-audit.log"
compose up -d --wait vibearr celery-worker celery-beat > "$output/start.log" 2>&1
if [[ $mode == full || $mode == unit ]]; then
  echo 'Running container application and runtime tests'
  compose run --rm --no-deps -e DJANGO_DEBUG=True tools sh -c 'cp -R /app /tmp/test-source && cd /tmp/test-source && python manage.py test "$@" --noinput' sh "$@" > "$output/application-tests.log" 2>&1
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true \
    --memory 2g --memory-swap 2g --pids-limit 128 --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,exec,size=2048m \
    --entrypoint sh "$VIBEARR_TOOLS_IMAGE" -c 'black --check . && ruff check . && mypy . --config-file mypy.ini && shellcheck test.sh run.sh deploy/docker/*.sh && actionlint -config-file .github/actionlint.yaml .github/workflows/*.yml && python -m unittest discover -s vibearr_executor -t . && python -m vibearr_runner.verify' > "$output/tools-tests.log" 2>&1
fi
if [[ $mode == full || $mode == boundary || $mode == candidate || $mode == unit ]]; then
  compose build agent-worker setup-worker validation-worker worker-gateway > "$output/worker-build.log" 2>&1
  agent_id=$(docker image inspect "$VIBEARR_AGENT_IMAGE" --format '{{.Id}}')
  worker_id=$(docker image inspect "$VIBEARR_WORKER_IMAGE" --format '{{.Id}}')
  database_id=$(docker image inspect postgres:17@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f --format '{{.Id}}')
  printf 'PORT=0\nVIBEARR_AGENT_IMAGE_ID=%s\nVIBEARR_WORKER_IMAGE_ID=%s\nVIBEARR_DATABASE_IMAGE_ID=%s\n' "$agent_id" "$worker_id" "$database_id" > "$scratch/compose.env"
  package_image=$project-packaging:checks
  docker build --file deploy/docker/Packaging.Dockerfile --build-arg "SOURCE_IMAGE=$source_image" --build-arg "WORKER_IMAGE=$VIBEARR_WORKER_IMAGE" --build-arg "TOOLS_IMAGE=$VIBEARR_TOOLS_IMAGE" --tag "$package_image" . > "$output/packaging-build.log" 2>&1
  package_id=$(docker image inspect "$package_image" --format '{{.Id}}')
  docker run --rm --network none --read-only --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add KILL --cap-add SETUID --cap-add SETGID --cap-add SETPCAP --security-opt no-new-privileges:true \
    --memory 2g --memory-swap 2g --pids-limit 128 --cpus 2 \
    --tmpfs /work:rw,nosuid,nodev,exec,size=1024m,uid=1000,gid=1000,mode=0700 \
    --tmpfs /home/task:rw,nosuid,nodev,exec,size=32m,uid=1000,gid=1000,mode=0700 \
    --tmpfs /tmp:rw,nosuid,nodev,exec,size=128m,mode=1777 --tmpfs /input:rw,nosuid,nodev,noexec,size=40m,mode=0711 \
    --tmpfs /secrets:rw,nosuid,nodev,noexec,size=64k,mode=0711 --tmpfs /control:rw,nosuid,nodev,noexec,size=128m,mode=0700 \
    --tmpfs /opt/candidate:rw,nosuid,nodev,exec,size=4m,mode=0755 \
    -e "VIBEARR_CHECK_IMAGE_ID=$package_id" -e "VIBEARR_CHECK_COMMIT=$commit" -e "VIBEARR_CHECK_BASE=$base" -e "VIBEARR_CHECK_INTRODUCTION=$introduction" "$package_image" > "$output/packaging.log" 2>&1
fi
if [[ $mode == full || $mode == boundary || $mode == candidate ]]; then
  if [[ -n $candidate && $mode == boundary ]]; then
    echo 'Building the exact implementation under test with the protected Dockerfiles'
    docker build --file "$recipe/vibearr_executor/Slot.Dockerfile" --target worker --tag "$VIBEARR_WORKER_IMAGE" "$runtime_source" > "$output/implementation-worker-build.log" 2>&1
    docker build --file "$recipe/vibearr_executor/Slot.Dockerfile" --target agent --tag "$VIBEARR_AGENT_IMAGE" "$runtime_source" >> "$output/implementation-worker-build.log" 2>&1
    docker build --file "$recipe/vibearr_runner/Dockerfile" --tag "$VIBEARR_RUNNER_IMAGE" "$runtime_source" >> "$output/implementation-worker-build.log" 2>&1
    agent_id=$(docker image inspect "$VIBEARR_AGENT_IMAGE" --format '{{.Id}}')
    worker_id=$(docker image inspect "$VIBEARR_WORKER_IMAGE" --format '{{.Id}}')
    printf 'PORT=0\nVIBEARR_AGENT_IMAGE_ID=%s\nVIBEARR_WORKER_IMAGE_ID=%s\nVIBEARR_DATABASE_IMAGE_ID=%s\n' "$agent_id" "$worker_id" "$database_id" > "$scratch/compose.env"
  fi
  echo 'Qualifying actual worker boots and resource enforcement'
  compose up -d --wait agent-worker setup-worker validation-worker worker-gateway > "$output/worker-start.log" 2>&1
  compose run --rm --no-deps qualification prepare > "$output/restart-witness.log" 2>&1
  compose kill -s SIGKILL agent-worker setup-worker validation-worker > "$output/restart.log" 2>&1
  compose up -d --wait agent-worker setup-worker validation-worker worker-gateway >> "$output/restart.log" 2>&1
  compose run --rm --no-deps qualification finish > "$output/qualification.log" 2>&1
  compose run --rm --no-deps --entrypoint cat qualification /run/vibearr/runner/docker-qualification.json > "$output/qualification.json"
  resource_container=$project-resource-check
  if docker container inspect "$resource_container" >/dev/null 2>&1; then
    echo 'The resource check container name already exists; select a new project.' >&2
    exit 2
  fi
  resource_status=0
  compose run --no-deps --name "$resource_container" resource-check > "$output/resource-check.log" 2>&1 || resource_status=$?
  if [[ $(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$resource_container") != "$project" ]]; then
    echo 'Resource observation does not belong to this check project.' >&2
    exit 2
  fi
  docker inspect --format '{"container_id":"{{.Id}}","image":"{{.Image}}","oom_killed":{{.State.OOMKilled}},"exit_code":{{.State.ExitCode}},"running":{{.State.Running}},"memory_bytes":{{.HostConfig.Memory}},"memory_swap_bytes":{{.HostConfig.MemorySwap}},"nano_cpus":{{.HostConfig.NanoCpus}},"pids":{{.HostConfig.PidsLimit}}}' "$resource_container" > "$output/resource-observation.json"
  docker rm "$resource_container" > "$output/resource-cleanup.log"
  if ((resource_status)); then exit "$resource_status"; fi
  ids=$(compose ps -q)
  # shellcheck disable=SC2086
  docker inspect $ids | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.audit --inspect > "$output/runtime-inspection.json"
fi
if [[ $mode == full || $mode == candidate ]]; then
  echo 'Validating candidate through the worker APIs'
  candidate_image=$project-controller:checks
  docker build --file deploy/docker/Candidate.Dockerfile --build-arg "SOURCE_IMAGE=$source_image" --build-arg "TOOLS_IMAGE=$VIBEARR_TOOLS_IMAGE" --tag "$candidate_image" . > "$output/controller-build.log" 2>&1
  source_digest=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.records source_digest < "$output/packaging.log")
  arguments=(--expected-source-digest "$source_digest")
  if [[ -n $commit ]]; then arguments+=(--commit "$commit"); fi
  if [[ -n $base ]]; then arguments+=(--base "$base"); fi
  if [[ -n $introduction ]]; then arguments+=(--introduction "$introduction"); fi
  VIBEARR_TOOLS_IMAGE=$candidate_image compose run --rm --no-deps --entrypoint python qualification -m deploy.docker.check_candidate "${arguments[@]}" > "$output/candidate.log" 2>&1
  compose run --rm --no-deps --entrypoint python qualification -m deploy.docker.validate --dependency > "$output/dependency-validation.log" 2>&1
fi
if [[ $mode == full || $mode == operations ]]; then
  echo 'Checking bootstrap, browser, backup and fenced restore'
  compose build browser > "$output/operations-build.log" 2>&1
  docker build --file "$recipe/deploy/docker/Backup.Dockerfile" --tag "$VIBEARR_BACKUP_IMAGE" "$runtime_source" >> "$output/operations-build.log" 2>&1
  docker build --file "$recipe/vibearr_runner/Dockerfile" --tag "$VIBEARR_RUNNER_IMAGE" "$runtime_source" >> "$output/operations-build.log" 2>&1
  compose run --rm --no-deps tools python manage.py docker_bootstrap_runner check-runner > "$output/bootstrap.log" 2>&1
  compose run --rm --no-deps tools python -m vibearr_runner.drain on > "$output/drain-request.log" 2>&1
  compose up --no-deps -d vibearr-runner > "$output/drain-start.log" 2>&1
  compose run --rm --no-deps tools python -m vibearr_runner.drain wait --timeout 60 > "$output/drain-ack.log" 2>&1
  compose stop vibearr-runner > "$output/drain-stop.log" 2>&1
  compose run --rm --no-deps -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 tools python -m deploy.docker.recovery_fixture seed > "$output/seed.log" 2>&1
  browser_arguments=()
  if [[ -n $candidate ]]; then
    application_container=$(compose ps -q vibearr)
    application_image=$(docker inspect --format '{{.Image}}' "$application_container")
    if [[ $application_image != "$(docker image inspect "$VIBEARR_APP_IMAGE" --format '{{.Id}}')" ]]; then
      echo 'The browser target differs from the exact candidate application image.' >&2
      exit 1
    fi
    browser_image=$(docker image inspect "$VIBEARR_BROWSER_IMAGE" --format '{{.Id}}')
    docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.browser_binding --application-image "$application_image" --browser-image "$browser_image" < "$output/implementation-source.json" > "$output/browser-binding.json"
    browser_binding=$(cat "$output/browser-binding.json")
    browser_arguments+=(-e "VIBEARR_BROWSER_SOURCE_BINDING=$browser_binding")
  fi
  compose run --rm --no-deps "${browser_arguments[@]}" browser > "$output/browser.log" 2>&1
  if [[ -n $candidate ]]; then
    browser_report=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.records report_path < "$output/browser.log")
    compose run --rm --no-deps --entrypoint python qualification -m vibearr_runner.browser_receipts --report "$browser_report" --run-id 11111111-1111-4111-8111-111111111111 --state /artifacts/browser-acceptance > "$output/browser-receipt-check.log" 2>&1
  fi
  compose stop vibearr celery-worker celery-beat agent-worker setup-worker validation-worker worker-gateway valkey > "$output/quiesce.log" 2>&1
  compose run --rm --no-deps backup backup --quiesced > "$output/backup.log" 2>&1
  backup_id=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.records backup_id < "$output/backup.log")
  restored run --rm --no-deps restore-files restore-files "$backup_id" > "$output/restore-files.log" 2>&1
  restored up --no-deps -d --wait postgres > "$output/restore-start.log" 2>&1
  restored run --rm --no-deps restore-database restore-database "$backup_id" > "$output/restore-database.log" 2>&1
  restored run --rm --no-deps -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 tools python -m deploy.docker.recovery_fixture verify > "$output/restore-verify.log" 2>&1
  restored run --rm --no-deps tools python manage.py docker_bootstrap_runner restored-check-runner --restored-backup "$backup_id" > "$output/restore-bootstrap.log" 2>&1
  restored up --no-deps -d --wait vibearr > "$output/restore-web.log" 2>&1
  restored run --rm --no-deps -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 tools python -m deploy.docker.restore_check "$backup_id" > "$output/restore-authority.log" 2>&1
fi
echo "Docker $mode checks passed."
