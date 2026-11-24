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
[[ -z $candidate || -n $commit ]] || { echo 'Exact candidate input requires --commit.' >&2; exit 2; }
project=${VIBEARR_CHECK_PROJECT:-vibearr-check-${GITHUB_RUN_ID:-local}-${RANDOM}-${RANDOM}}
[[ $project =~ ^vibearr-check-[a-z0-9-]+$ && ${#project} -le 50 ]] || { echo 'Choose a disposable vibearr-check-* project of at most 50 characters.' >&2; exit 2; }
restore_project=${project}-restore
for selected in "$project" "$restore_project"; do
  [[ -z $(docker ps -aq --filter "label=com.docker.compose.project=$selected") && -z $(docker volume ls -q --filter "name=^${selected}_") ]] || { echo 'Check project already has state; choose another name.' >&2; exit 2; }
done
scratch=$(mktemp -d /tmp/vibearr-check.XXXXXXXX)
output=${VIBEARR_CHECK_OUTPUT:-/tmp/$project-reports}
mkdir "$output"
export BUILDX_CONFIG=${BUILDX_CONFIG:-$scratch/buildx}
unset VIBEARR_API_TOKEN NQ_API_TOKEN VIBEARR_POSTGRES_PASSWORD POSTGRES_PASSWORD
unset VIBEARR_CREDENTIAL_KEY NQ_CREDENTIAL_KEY DJANGO_SECRET_KEY VIBEARR_RUNNER_API_TOKEN NQ_RUNNER_API_TOKEN
unset COMPOSE_PROFILES COMPOSE_ENV_FILES
export COMPOSE_DISABLE_ENV_FILE=1 COMPOSE_PROJECT_NAME=$project VIBEARR_DATA_PROJECT=$project
export PORT=0 VIBEARR_BIND=127.0.0.1 DJANGO_ALLOWED_HOSTS='*'
export VIBEARR_POSTGRES_DB=vibearr_checks VIBEARR_POSTGRES_USER=vibearr_checks
export VIBEARR_RUNNER_NAME=check-runner
export VIBEARR_APP_IMAGE=$project-app:checks VIBEARR_TOOLS_IMAGE=$project-tools:checks
export VIBEARR_RUNNER_IMAGE=$project-runner:checks VIBEARR_BACKUP_IMAGE=$project-backup:checks
export VIBEARR_BROWSER_IMAGE=$project-browser:checks VIBEARR_BACKUP_VOLUME=${project}_backup-data
export VIBEARR_RESTORE_BACKUP_VOLUME=${project}_backup-data
compose() { docker compose --file "$recipe/docker-compose.yml" --project-name "$project" "$@"; }
start_runner() {
  local attempt
  for attempt in 1 2 3 4 5 6; do
    if compose up -d --wait runner; then return; fi
    echo "Runner restart attempt $attempt observed an earlier exit; waiting for its new boot." >&2
    sleep 1
  done
  return 1
}
restored() { COMPOSE_PROJECT_NAME=$restore_project VIBEARR_DATA_PROJECT=$restore_project VIBEARR_RUNNER_NAME=restored-check-runner docker compose --file "$recipe/docker-compose.yml" --project-name "$restore_project" "$@"; }
export_volume() {
  local volume=$1 destination=$2
  if ! docker volume inspect "$volume" >/dev/null 2>&1; then return; fi
  mkdir -p "$destination"
  docker run --rm --network none --read-only --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true --user 0:0 --volume "$volume:/data:ro" --entrypoint tar "$VIBEARR_TOOLS_IMAGE" -C /data -cf - . | tar -xf - -C "$destination"
}
cleanup() {
  local result=$?
  trap - EXIT
  compose logs --no-color --tail 100 > "$output/services.log" 2>&1 || true
  export_volume "${project}_browser-artifacts" "$output/browser" || result=1
  restored down --volumes --remove-orphans > "$output/restore-cleanup.log" 2>&1 || result=1
  compose down --volumes --remove-orphans > "$output/cleanup.log" 2>&1 || result=1
  for volume in "${project}_backup-data" "${project}_browser-artifacts"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then docker volume rm "$volume" >> "$output/cleanup.log" 2>&1 || result=1; fi
  done
  for role in app tools runner source input packaging browser backup; do
    image=$project-$role:checks
    if docker image inspect "$image" >/dev/null 2>&1; then
      docker image rm --no-prune "$image" >> "$output/image-cleanup.log" 2>&1 || result=1
    fi
  done
  rm -rf "$scratch"
  echo "Docker check artifacts: $output"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Building disposable three-container checks for $project"
docker build --target tools --tag "$VIBEARR_TOOLS_IMAGE" . > "$output/tools-build.log" 2>&1
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
  if [[ -f $git_directory/commondir ]]; then IFS= read -r reference < "$git_directory/commondir"; common=$(realpath "$git_directory/$reference"); fi
  mkdir -p "$scratch/input/git/refs"
  cp -R "$common/objects" "$scratch/input/git/objects"
  cp -R "$common/refs/." "$scratch/input/git/refs/"
  cp "$git_directory/HEAD" "$scratch/input/git/HEAD"
  if [[ -f $common/packed-refs ]]; then cp "$common/packed-refs" "$scratch/input/git/packed-refs"; else : > "$scratch/input/git/packed-refs"; fi
  docker build --file deploy/docker/GitInput.Dockerfile --tag "$source_image" "$scratch/input" > "$output/source-build.log" 2>&1
else
  docker build --file deploy/docker/WorktreeInput.Dockerfile --tag "$source_image" . > "$output/source-build.log" 2>&1
fi
input_image=$project-input:checks
docker build --file deploy/docker/Candidate.Dockerfile --build-arg "SOURCE_IMAGE=$source_image" --build-arg "TOOLS_IMAGE=$VIBEARR_TOOLS_IMAGE" --tag "$input_image" . > "$output/input-build.log" 2>&1
arguments=()
if [[ -n $commit ]]; then arguments+=(--commit "$commit"); fi
if [[ -n $base ]]; then arguments+=(--base "$base"); fi
if [[ -n $introduction ]]; then arguments+=(--introduction "$introduction"); fi
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 512m --pids-limit 64 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=128m --entrypoint python "$input_image" -m deploy.docker.export_candidate "${arguments[@]}" > "$scratch/source.tar" 2> "$output/source.json"
mkdir "$scratch/source"
tar -xf "$scratch/source.tar" -C "$scratch/source"
for target in application runner; do
  image=$VIBEARR_APP_IMAGE
  if [[ $target == runner ]]; then image=$VIBEARR_RUNNER_IMAGE; fi
  docker build --file "$recipe/Dockerfile" --target "$target" --tag "$image" "$scratch/source" > "$output/$target-build.log" 2>&1
done
VIBEARR_RUNNER_IMAGE_ID=$(docker image inspect "$VIBEARR_RUNNER_IMAGE" --format '{{.Id}}')
export VIBEARR_RUNNER_IMAGE_ID
compose config --format json | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.audit > "$output/compose-audit.log"
compose config --format json | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp:rw,nosuid,nodev,size=8m --entrypoint python "$VIBEARR_TOOLS_IMAGE" -c 'import os,sys,unittest; from pathlib import Path; Path("/tmp/compose.json").write_bytes(sys.stdin.buffer.read()); os.environ["VIBEARR_COMPOSE_CONFIG"]="/tmp/compose.json"; result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromName("vibearr.test_compose")); sys.exit(not result.wasSuccessful())' > "$output/compose-tests.log" 2>&1
compose up -d --wait > "$output/start.log" 2>&1
mapfile -t ids < <(compose ps -q)
((${#ids[@]} == 3))
docker inspect "${ids[@]}" | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.audit --inspect > "$output/runtime-inspection.json"
if [[ $mode == full || $mode == unit ]]; then
  docker run --rm --network "${project}_database" --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 --memory 2g --pids-limit 128 --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,exec,size=2048m --volume "${project}_application-secrets:/run/vibearr/application:ro" --volume "${project}_database-secrets:/run/vibearr/database:ro" -e DJANGO_DEBUG=True -e POSTGRES_HOST=postgres -e "VIBEARR_POSTGRES_DB=$VIBEARR_POSTGRES_DB" -e "VIBEARR_POSTGRES_USER=$VIBEARR_POSTGRES_USER" --entrypoint sh "$VIBEARR_TOOLS_IMAGE" -c 'cp -R /app /tmp/test-source && cd /tmp/test-source && if [ "$#" -gt 0 ]; then set -- "$@" deploy.docker; fi; python -m deploy.docker.entrypoint python manage.py test "$@" --noinput' sh "$@" > "$output/application-tests.log" 2>&1
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 2g --pids-limit 128 --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,exec,size=2048m --entrypoint sh "$VIBEARR_TOOLS_IMAGE" -c 'black --check . && ruff check . && mypy . --config-file mypy.ini && shellcheck test.sh run.sh deploy/docker/*.sh && actionlint -config-file .github/actionlint.yaml .github/workflows/*.yml && python -m unittest discover -s vibearr_executor -t . && python -m vibearr_runner.verify' > "$output/tools-tests.log" 2>&1
fi
if [[ $mode == full || $mode == unit || $mode == candidate ]]; then
  package_image=$project-packaging:checks
  docker build --file deploy/docker/Packaging.Dockerfile --build-arg "SOURCE_IMAGE=$source_image" --build-arg "WORKER_IMAGE=$VIBEARR_RUNNER_IMAGE" --build-arg "TOOLS_IMAGE=$VIBEARR_TOOLS_IMAGE" --tag "$package_image" . > "$output/packaging-build.log" 2>&1
  package_id=$(docker image inspect "$package_image" --format '{{.Id}}')
  docker run --rm --network none --read-only --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add KILL --cap-add SETUID --cap-add SETGID --cap-add SETPCAP --security-opt no-new-privileges:true --memory 2g --memory-swap 2g --pids-limit 128 --cpus 2 --tmpfs /work:rw,nosuid,nodev,exec,size=1024m,uid=1000,gid=1000,mode=0700 --tmpfs /home/task:rw,nosuid,nodev,exec,size=32m,uid=1000,gid=1000,mode=0700 --tmpfs /tmp:rw,nosuid,nodev,exec,size=128m --tmpfs /input:rw,nosuid,nodev,noexec,size=40m,mode=0711 --tmpfs /secrets:rw,nosuid,nodev,noexec,size=64k,mode=0711 --tmpfs /control:rw,nosuid,nodev,noexec,size=128m,mode=0700 --tmpfs /opt/candidate:rw,nosuid,nodev,exec,size=4m,mode=0755 -e "VIBEARR_CHECK_IMAGE_ID=$package_id" -e "VIBEARR_CHECK_COMMIT=$commit" -e "VIBEARR_CHECK_BASE=$base" -e "VIBEARR_CHECK_INTRODUCTION=$introduction" "$package_image" > "$output/packaging.log" 2>&1
fi
if [[ $mode == full || $mode == boundary || $mode == candidate ]]; then
  compose exec -T runner python -I -m vibearr_executor.integrated restart-witness > "$output/restart-witness.json" 2>&1
  compose kill -s SIGKILL runner > "$output/restart.log" 2>&1
  start_runner >> "$output/restart.log" 2>&1
  compose exec -T runner python -I -m vibearr_executor.integrated qualify > "$output/qualification.json" 2>&1
  compose exec -T runner python -I -m vibearr_executor.integrated restart-witness > "$output/second-witness.json" 2>&1
  compose kill -s SIGKILL runner >> "$output/restart.log" 2>&1
  start_runner >> "$output/restart.log" 2>&1
  compose exec -T runner python -I -c 'from pathlib import Path; from vibearr_executor.client import Client; c=Client("http://127.0.0.1:8791",Path("/run/vibearr/runner/control-token").read_text().strip()); assert c.capabilities()["qualified"] is False' > "$output/restart-proof.log" 2>&1
  compose exec -T runner python -I -m vibearr_executor.integrated qualify > "$output/requalified.json" 2>&1
  compose exec -T runner python -I < deploy/docker/dependency_smoke.py > "$output/dependency-smoke.json" 2>&1
fi
if [[ $mode == full || $mode == candidate ]]; then
  mapfile -t binding < <(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -c 'import json,sys; v=json.load(sys.stdin); print(v["commit_sha"]); print(v["tree_sha"]); print(v["source_digest"])' < "$output/source.json")
  candidate_result=0
  compose exec -T runner python -I -m vibearr_executor.integrated_validate --commit "${binding[0]}" --tree "${binding[1]}" --source-digest "${binding[2]}" < "$scratch/source.tar" > "$output/candidate.log" 2>&1 || candidate_result=$?
  container=$(compose ps -q runner)
  docker cp "$container:/control/candidate-check" "$output/candidate" > "$output/candidate-export.log" 2>&1
  ((candidate_result == 0)) || exit "$candidate_result"
fi
if [[ $mode == full || $mode == operations ]]; then
  if [[ -n $commit ]]; then
    docker build --file deploy/docker/Browser.Dockerfile --tag "$VIBEARR_BROWSER_IMAGE" . > "$output/browser-build.log" 2>&1
    application_image=$(docker image inspect "$VIBEARR_APP_IMAGE" --format '{{.Id}}')
    browser_image=$(docker image inspect "$VIBEARR_BROWSER_IMAGE" --format '{{.Id}}')
    VIBEARR_BROWSER_SOURCE_BINDING=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.browser_binding --application-image "$application_image" --browser-image "$browser_image" < "$output/source.json")
    export VIBEARR_BROWSER_SOURCE_BINDING
  fi
  bash deploy/docker/operations.sh browser > "$output/browser.log" 2>&1
  compose stop app runner > "$output/quiesce.log" 2>&1
  bash deploy/docker/operations.sh backup --quiesced > "$output/backup.log" 2>&1
  backup_id=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.records backup_id < "$output/backup.log")
  for action in restore-files restore-database restore-bootstrap; do
    COMPOSE_PROJECT_NAME=$restore_project VIBEARR_DATA_PROJECT=$restore_project VIBEARR_RUNNER_NAME=restored-check-runner bash deploy/docker/operations.sh "$action" "$backup_id" > "$output/$action.log" 2>&1
  done
  restored up -d --wait > "$output/restored-start.log" 2>&1
  restored exec -T --user 10001:10001 app python -m deploy.docker.entrypoint python manage.py check > "$output/restored-system.log" 2>&1
fi
