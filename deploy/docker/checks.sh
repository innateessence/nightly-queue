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
[[ $project =~ ^(vibearr-check|nq-auto-qualification)-[a-z0-9-]+$ && ${#project} -le 50 ]] || { echo 'Choose a disposable vibearr-check-* or nq-auto-qualification-* project of at most 50 characters.' >&2; exit 2; }
restore_project=${project}-restore
for selected in "$project" "$restore_project"; do
  existing_containers=$(docker ps -aq --filter "label=com.docker.compose.project=$selected") || exit $?
  existing_volumes=$(docker volume ls -q --filter "name=^${selected}_") || exit $?
  [[ -z $existing_containers && -z $existing_volumes ]] || { echo 'Check project already has state; choose another name.' >&2; exit 2; }
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
export VIBEARR_WORKER_BASE_IMAGE=$project-worker-base:checks VIBEARR_WORKER_IMAGE=$project-worker:checks
export VIBEARR_BROWSER_IMAGE=$project-browser:checks VIBEARR_BACKUP_VOLUME=${project}_backup-data
export VIBEARR_RESTORE_BACKUP_VOLUME=${project}_backup-data
compose() { docker compose --file "$recipe/docker-compose.yml" --project-name "$project" "$@"; }
start_runtime() {
  local attempt
  local orchestrate=${1:-compose}
  for attempt in 1 2 3 4 5 6; do
    if "$orchestrate" up -d --wait worker runner; then return; fi
    echo "Runtime restart attempt $attempt observed an earlier exit; waiting for its new boot." >&2
    sleep 1
  done
  return 1
}
restored() { COMPOSE_PROJECT_NAME=$restore_project VIBEARR_DATA_PROJECT=$restore_project VIBEARR_RUNNER_NAME=restored-check-runner docker compose --file "$recipe/docker-compose.yml" --project-name "$restore_project" "$@"; }
qualify_worker() {
  local orchestrate=${1:-compose}
  local status=0
  local targets
  local selected_listing
  local selected_ids=()
  printf 'Qualification stage=select orchestrator=%s\n' "$orchestrate" >&2
  selected_listing=$("$orchestrate" ps -q runner app postgres) || { status=$?; printf 'Qualification stage=select status=%s\n' "$status" >&2; return "$status"; }
  mapfile -t selected_ids <<< "$selected_listing"
  printf 'Qualification stage=inspect-targets orchestrator=%s\n' "$orchestrate" >&2
  targets=$(docker inspect "${selected_ids[@]}" | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$VIBEARR_TOOLS_IMAGE" -I -c '
import ipaddress,json,sys
containers=json.load(sys.stdin)
networks={"runner":"worker-control","app":"runner-api","postgres":"database"}
targets={}
projects=set()
for container in containers:
    labels=container["Config"]["Labels"]
    service=labels["com.docker.compose.service"]
    project=labels["com.docker.compose.project"]
    address=container["NetworkSettings"]["Networks"][project+"_"+networks[service]]["IPAddress"]
    parsed=ipaddress.ip_address(address)
    if parsed.version != 4 or not parsed.is_private or parsed.is_loopback or service in targets:
        raise ValueError("Qualification requires distinct current private service addresses")
    targets[service]=address
    projects.add(project)
if set(targets) != set(networks) or len(projects) != 1:
    raise ValueError("Qualification requires one complete selected Compose project")
print(json.dumps(targets,sort_keys=True))
') || { status=$?; printf 'Qualification stage=inspect-targets status=%s\n' "$status" >&2; return "$status"; }
  printf 'Qualification stage=qualify-cli orchestrator=%s\n' "$orchestrate" >&2
  "$orchestrate" exec -T -e "VIBEARR_QUALIFICATION_TARGETS=$targets" worker python -I -m vibearr_executor.compact_worker qualify || status=$?
  printf 'Qualification stage=qualify-cli status=%s\n' "$status" >&2
  return "$status"
}
observe_phase() {
  local phase=$1
  local phase_listing
  local phase_ids=()
  phase_listing=$(docker ps -q --no-trunc --filter "label=com.docker.compose.project=$project" --filter status=running) || return
  mapfile -t phase_ids <<< "$phase_listing"
  ((${#phase_ids[@]} == 4)) || { echo "Expected four running services after $phase." >&2; return 1; }
  printf '%s\n' "${phase_ids[@]}" | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$VIBEARR_TOOLS_IMAGE" -I -c 'import json,re,sys; ids=sys.stdin.read().splitlines(); assert len(set(ids)) == 4 and all(re.fullmatch("[a-f0-9]{64}", value) for value in ids); print(json.dumps({"phase":sys.argv[1],"containers":ids},sort_keys=True))' "$phase" >> "$output/phase-counts.jsonl"
}
export_volume() {
  local volume=$1 destination=$2
  if ! docker volume inspect "$volume" >/dev/null 2>&1; then return; fi
  mkdir -p "$destination"
  docker run --rm --network none --read-only --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true --user 0:0 --memory 128m --pids-limit 16 --cpus 1 --volume "$volume:/data:ro" --entrypoint tar "$VIBEARR_TOOLS_IMAGE" -C /data -cf - . | tar -xf - -C "$destination"
}
failure_state() {
  local orchestrate=$1 listing
  local selected_ids=()
  listing=$("$orchestrate" ps -aq worker runner) || return
  [[ -n $listing ]] || return 0
  mapfile -t selected_ids <<< "$listing"
  ((${#selected_ids[@]} <= 2)) || return 1
  docker inspect --format '{"id":{{json .Id}},"image":{{json .Image}},"status":{{json .State.Status}},"running":{{.State.Running}},"oom_killed":{{.State.OOMKilled}},"exit_code":{{.State.ExitCode}},"started_at":{{json .State.StartedAt}},"finished_at":{{json .State.FinishedAt}},"restart_count":{{.RestartCount}}}' "${selected_ids[@]}"
}
cleanup() {
  local original_status=$?
  local result=$original_status cleanup_status=0
  trap - EXIT
  if ((original_status != 0)); then
    failure_state compose > "$output/failure-compose-state.jsonl" 2> "$output/failure-compose-state.log" || true
    failure_state restored > "$output/failure-restored-state.jsonl" 2> "$output/failure-restored-state.log" || true
  fi
  compose logs --no-color --tail 100 > "$output/services.log" 2>&1 || true
  export_volume "${project}_browser-artifacts" "$output/browser" || cleanup_status=1
  restored down --volumes --remove-orphans > "$output/restore-cleanup.log" 2>&1 || cleanup_status=1
  compose down --volumes --remove-orphans > "$output/cleanup.log" 2>&1 || cleanup_status=1
  for volume in "${project}_backup-data" "${project}_browser-artifacts"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then docker volume rm "$volume" >> "$output/cleanup.log" 2>&1 || cleanup_status=1; fi
  done
  for role in app tools runner worker worker-base source input packaging browser backup reports; do
    image=$project-$role:checks
    if docker image inspect "$image" >/dev/null 2>&1; then
      docker image rm --no-prune "$image" >> "$output/image-cleanup.log" 2>&1 || cleanup_status=1
    fi
  done
  rm -rf "$scratch" || cleanup_status=1
  if ((result == 0)); then result=$cleanup_status; fi
  if ! printf '{"original_status":%s,"cleanup_status":%s,"final_status":%s}\n' "$original_status" "$cleanup_status" "$result" > "$output/exit-status.json"; then
    if ((result == 0)); then result=1; fi
    printf 'Unable to record check exit status.\n' >&2 || true
  fi
  echo "Docker check artifacts: $output"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "Building disposable four-container checks for $project"
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
# Runtime, tooling and recipes all come from the exported candidate.
for target in tools application runner worker-base; do
  image=$VIBEARR_APP_IMAGE
  if [[ $target == tools ]]; then image=$VIBEARR_TOOLS_IMAGE; fi
  if [[ $target == runner ]]; then image=$VIBEARR_RUNNER_IMAGE; fi
  if [[ $target == worker-base ]]; then image=$VIBEARR_WORKER_BASE_IMAGE; fi
  docker build --file "$scratch/source/Dockerfile" --target "$target" --tag "$image" "$scratch/source" > "$output/$target-build.log" 2>&1
done
VIBEARR_WORKER_BASE_IMAGE_ID=$(docker image inspect "$VIBEARR_WORKER_BASE_IMAGE" --format '{{.Id}}')
export VIBEARR_WORKER_BASE_IMAGE_ID
docker build --file "$scratch/source/vibearr_executor/Worker.Dockerfile" --build-arg "RUNTIME_IMAGE=$VIBEARR_WORKER_BASE_IMAGE" --build-arg "RUNTIME_IMAGE_ID=$VIBEARR_WORKER_BASE_IMAGE_ID" --tag "$VIBEARR_WORKER_IMAGE" "$scratch/source" > "$output/worker-build.log" 2>&1
worker_base_after=$(docker image inspect "$VIBEARR_WORKER_BASE_IMAGE" --format '{{.Id}}') || exit $?
[[ $worker_base_after == "$VIBEARR_WORKER_BASE_IMAGE_ID" ]] || { echo 'Worker toolchain image changed during build.' >&2; exit 1; }
VIBEARR_RUNNER_IMAGE_ID=$(docker image inspect "$VIBEARR_RUNNER_IMAGE" --format '{{.Id}}')
VIBEARR_WORKER_IMAGE_ID=$(docker image inspect "$VIBEARR_WORKER_IMAGE" --format '{{.Id}}')
export VIBEARR_RUNNER_IMAGE_ID VIBEARR_WORKER_IMAGE_ID
recipe=$scratch/source
cd "$recipe"
compose config --format json | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.audit > "$output/compose-audit.log"
compose config --format json | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=8m --entrypoint python "$VIBEARR_TOOLS_IMAGE" -c 'import os,sys,unittest; from pathlib import Path; Path("/tmp/compose.json").write_bytes(sys.stdin.buffer.read()); os.environ["VIBEARR_COMPOSE_CONFIG"]="/tmp/compose.json"; result=unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromName("vibearr.test_compose")); sys.exit(not result.wasSuccessful())' > "$output/compose-tests.log" 2>&1
compose up -d --wait > "$output/start.log" 2>&1
id_listing=$(compose ps -q) || exit $?
mapfile -t ids <<< "$id_listing"
((${#ids[@]} == 4))
docker inspect "${ids[@]}" | docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.audit --inspect > "$output/runtime-inspection.json"
docker stats --no-stream --no-trunc --format '{{json .}}' "${ids[@]}" > "$output/idle-resources.jsonl"
observe_phase startup
if [[ $mode == full || $mode == unit ]]; then
  docker run --rm --network "${project}_database" --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 --memory 2g --pids-limit 128 --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,exec,size=2048m --volume "${project}_application-secrets:/run/vibearr/application:ro" --volume "${project}_database-secrets:/run/vibearr/database:ro" -e DJANGO_DEBUG=True -e POSTGRES_HOST=postgres -e "VIBEARR_POSTGRES_DB=$VIBEARR_POSTGRES_DB" -e "VIBEARR_POSTGRES_USER=$VIBEARR_POSTGRES_USER" --entrypoint sh "$VIBEARR_TOOLS_IMAGE" -c 'cp -R /app /tmp/test-source && cd /tmp/test-source && if [ "$#" -gt 0 ]; then set -- "$@" deploy.docker; fi; python -m deploy.docker.entrypoint python manage.py test "$@" --noinput' sh "$@" > "$output/application-tests.log" 2>&1
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 2g --pids-limit 128 --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,exec,size=2048m --entrypoint sh "$VIBEARR_TOOLS_IMAGE" -c 'black --check . && ruff check . && mypy . --config-file mypy.ini && shellcheck test.sh run.sh deploy/docker/*.sh && actionlint -config-file .github/actionlint.yaml .github/workflows/*.yml && python -m unittest discover -s vibearr_executor -t . && cp -R /app /tmp/portable-source && cd /tmp/portable-source && python -m vibearr_runner.verify' > "$output/tools-tests.log" 2>&1
fi
if [[ $mode == full || $mode == unit || $mode == candidate ]]; then
  package_image=$project-packaging:checks
  docker build --file "$recipe/deploy/docker/Packaging.Dockerfile" --build-arg "SOURCE_IMAGE=$source_image" --build-arg "WORKER_IMAGE=$VIBEARR_WORKER_IMAGE" --build-arg "TOOLS_IMAGE=$VIBEARR_TOOLS_IMAGE" --tag "$package_image" "$recipe" > "$output/packaging-build.log" 2>&1
  package_id=$(docker image inspect "$package_image" --format '{{.Id}}')
  docker run --rm --network none --read-only --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add KILL --cap-add SETUID --cap-add SETGID --cap-add SETPCAP --security-opt no-new-privileges:true --memory 2g --memory-swap 2g --pids-limit 128 --cpus 2 --tmpfs /work:rw,nosuid,nodev,exec,size=1024m,uid=1000,gid=1000,mode=0700 --tmpfs /home/task:rw,nosuid,nodev,exec,size=32m,uid=1000,gid=1000,mode=0700 --tmpfs /tmp:rw,nosuid,nodev,exec,size=128m --tmpfs /input:rw,nosuid,nodev,noexec,size=40m,mode=0711 --tmpfs /secrets:rw,nosuid,nodev,noexec,size=64k,mode=0711 --tmpfs /control:rw,nosuid,nodev,noexec,size=128m,mode=0700 --tmpfs /opt/candidate:rw,nosuid,nodev,exec,size=4m,mode=0755 -e "VIBEARR_CHECK_IMAGE_ID=$package_id" -e "VIBEARR_CHECK_COMMIT=$commit" -e "VIBEARR_CHECK_BASE=$base" -e "VIBEARR_CHECK_INTRODUCTION=$introduction" "$package_image" > "$output/packaging.log" 2>&1
fi
if [[ $mode == full || $mode == boundary || $mode == candidate || $mode == operations ]]; then
  compose exec -T worker python -I -m vibearr_executor.compact_worker restart-witness > "$output/restart-witness.json" 2>&1
  compose kill -s SIGKILL worker > "$output/restart.log" 2>&1
  start_runtime >> "$output/restart.log" 2>&1
  qualify_worker > "$output/qualification.json" 2>> "$output/qualification-stages.log"
  compose exec -T worker python -I -m vibearr_executor.compact_worker restart-witness > "$output/second-witness.json" 2>&1
  compose kill -s SIGKILL worker >> "$output/restart.log" 2>&1
  start_runtime >> "$output/restart.log" 2>&1
  compose exec -T worker python -I -c 'from pathlib import Path; from vibearr_executor.client import Client; c=Client("http://127.0.0.1:8791",Path("/run/vibearr/worker/control-token").read_text().strip()); assert c.capabilities()["qualified"] is False' > "$output/restart-proof.log" 2>&1
  qualify_worker > "$output/requalified.json" 2>> "$output/qualification-stages.log"
  compose exec -T worker python -I -m vibearr_executor.compact_worker qualify-roles > "$output/role-qualification.json" 2>&1
  observe_phase boundary
  compose exec -T worker python -I < deploy/docker/dependency_smoke.py > "$output/dependency-smoke.json" 2>&1
  worker_container=$(compose ps -q worker) || exit $?
  docker cp "$worker_container:/control/dependency-smoke-proof.json" "$output/dependency-smoke-proof.json" > "$output/dependency-smoke-export.log" 2>&1
  observe_phase dependency
  runner_container=$(compose ps -q runner) || exit $?
  docker inspect "$runner_container" --format '{{json .}}' > "$output/runner-before.json"
  compose exec -T -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 runner python -I - < deploy/docker/controller_witness.py > "$output/controller-before.json" 2>&1
  compose kill -s SIGKILL runner > "$output/runner-restart.log" 2>&1
  start_runtime >> "$output/runner-restart.log" 2>&1
  runner_container=$(compose ps -q runner) || exit $?
  docker inspect "$runner_container" --format '{{json .}}' > "$output/runner-after.json"
  compose exec -T -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 runner python -I - < deploy/docker/controller_witness.py > "$output/controller-after.json" 2>&1
fi
if [[ $mode == full || $mode == candidate ]]; then
  binding_listing=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$VIBEARR_TOOLS_IMAGE" -c 'import json,sys; v=json.load(sys.stdin); print(v["commit_sha"]); print(v["tree_sha"]); print(v["source_digest"])' < "$output/source.json") || exit $?
  mapfile -t binding <<< "$binding_listing"
  candidate_result=0
  compose exec -T worker python -I -m vibearr_executor.integrated_validate --commit "${binding[0]}" --tree "${binding[1]}" --source-digest "${binding[2]}" < "$scratch/source.tar" > "$output/candidate.log" 2>&1 || candidate_result=$?
  container=$(compose ps -q worker)
  docker cp "$container:/control/candidate-check" "$output/candidate" > "$output/candidate-export.log" 2>&1
  ((candidate_result == 0)) || exit "$candidate_result"
  observe_phase candidate
  browser_result=0
  compose exec -T worker python -I -m vibearr_executor.compact_browser_qualify --commit "${binding[0]}" --tree "${binding[1]}" --source-digest "${binding[2]}" < "$scratch/source.tar" > "$output/candidate-browser.log" 2>&1 || browser_result=$?
  docker cp "$container:/control/candidate-browser" "$output/candidate-browser" > "$output/candidate-browser-export.log" 2>&1
  ((browser_result == 0)) || exit "$browser_result"
  observe_phase browser
  compose exec -T worker python -I -m vibearr_executor.compact_proof_export > "$output/proof-export.log" 2>&1
  docker cp "$container:/control/engineering-proofs" "$output/engineering-proofs" > "$output/proof-copy.log" 2>&1
fi
if [[ $mode == full || $mode == operations ]]; then
  if [[ -n $commit ]]; then
    docker build --file deploy/docker/Browser.Dockerfile --tag "$VIBEARR_BROWSER_IMAGE" . > "$output/browser-build.log" 2>&1
    application_image=$(docker image inspect "$VIBEARR_APP_IMAGE" --format '{{.Id}}')
    browser_image=$(docker image inspect "$VIBEARR_BROWSER_IMAGE" --format '{{.Id}}')
    VIBEARR_BROWSER_SOURCE_BINDING=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.browser_binding --application-image "$application_image" --browser-image "$browser_image" < "$output/source.json")
    export VIBEARR_BROWSER_SOURCE_BINDING
  fi
  bash deploy/docker/operations.sh browser > "$output/browser.log" 2>&1
  compose exec -T -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 runner python -I - seed-state < deploy/docker/restore_check.py > "$output/recovery-private-seed.json" 2>&1
  compose exec -T --user 10001:10001 -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 app python -m deploy.docker.entrypoint python -m deploy.docker.recovery_fixture seed > "$output/recovery-seed.json" 2>&1
  compose exec -T --user 10001:10001 runner python -I -m vibearr_runner.drain on > "$output/drain-request.json" 2>&1
  compose exec -T --user 10001:10001 runner python -I -m vibearr_runner.drain wait --timeout 60 > "$output/drain-ack.json" 2>&1
  observe_phase beforebackup
  compose stop app runner worker > "$output/quiesce.log" 2>&1
  bash deploy/docker/operations.sh backup --quiesced > "$output/backup.log" 2>&1
  backup_id=$(docker run --rm -i --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --memory 128m --pids-limit 16 --cpus 1 --entrypoint python "$VIBEARR_TOOLS_IMAGE" -m deploy.docker.records backup_id < "$output/backup.log")
  docker run --rm --network none --read-only --cap-drop ALL --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true --user 0:0 --memory 128m --pids-limit 16 --cpus 1 --volume "$VIBEARR_BACKUP_VOLUME:/backups:ro" --entrypoint python "$VIBEARR_TOOLS_IMAGE" -I -c 'import json,sys; from pathlib import Path; from uuid import UUID; value=sys.argv[1]; assert str(UUID(value)) == value; path=Path("/backups") / value / "manifest.json"; assert path.is_file() and not path.is_symlink() and path.stat().st_size <= 1048576; print(json.dumps(json.loads(path.read_bytes()),sort_keys=True))' "$backup_id" > "$output/backup-manifest.json"
  for action in restore-files restore-database restore-bootstrap; do
    COMPOSE_PROJECT_NAME=$restore_project VIBEARR_DATA_PROJECT=$restore_project VIBEARR_RUNNER_NAME=restored-check-runner bash deploy/docker/operations.sh "$action" "$backup_id" > "$output/$action.log" 2>&1
  done
  restored up -d --wait > "$output/restored-start.log" 2>&1
  restored exec -T --user 10001:10001 app python -m deploy.docker.entrypoint python manage.py check > "$output/restored-system.log" 2>&1
  restored exec -T --user 10001:10001 -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 app python -m deploy.docker.entrypoint python -m deploy.docker.recovery_fixture verify > "$output/restored-fixture.json" 2>&1
  restored exec -T -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 runner python -I - "$backup_id" < deploy/docker/restore_check.py > "$output/restored-authority.json" 2>&1
  restored exec -T worker python -I -m vibearr_executor.compact_worker restart-witness > "$output/restored-restart-witness.json" 2>&1
  restored kill -s SIGKILL worker > "$output/restored-restart.log" 2>&1
  start_runtime restored >> "$output/restored-restart.log" 2>&1
  qualify_worker restored > "$output/restored-qualification.json" 2>> "$output/qualification-stages.log"
  restored exec -T worker python -I -m vibearr_executor.compact_worker qualify-roles > "$output/restored-role-qualification.json" 2>&1
  restored exec -T -e VIBEARR_DISPOSABLE_ACCEPTANCE=1 worker python -I - "$backup_id" --worker < deploy/docker/restore_check.py > "$output/restored-worker-authority.json" 2>&1
  restored exec -T worker python -I -c 'import json,os; from pathlib import Path; from vibearr_executor.client import Client; c=Client("http://127.0.0.1:8791",Path(os.environ["VIBEARR_EXECUTOR_TOKEN_FILE"]).read_text().strip()); print(json.dumps(c.capabilities(),sort_keys=True))' > "$output/restored-capabilities.json"
  restored exec -T worker python -I -c 'import json,os; from pathlib import Path; from vibearr_executor.client import Client; c=Client("http://127.0.0.1:8791",Path(os.environ["VIBEARR_EXECUTOR_TOKEN_FILE"]).read_text().strip()); print(json.dumps(c.request("/v4/qualification"),sort_keys=True))' > "$output/restored-boundary.json"
fi
if [[ $mode == full && -n $commit ]]; then
  report_image=$project-reports:checks
  docker build --file "$recipe/deploy/docker/Reports.Dockerfile" --build-arg "TOOLS_IMAGE=$VIBEARR_TOOLS_IMAGE" --tag "$report_image" "$output" > "$scratch/reports-build.log" 2>&1
  mv "$scratch/reports-build.log" "$output/reports-build.log"
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 --memory 512m --pids-limit 64 --cpus 1 --tmpfs /tmp:rw,nosuid,nodev,size=128m "$report_image" > "$output/engineering-report.json" 2> "$output/engineering-report-validation.log"
elif [[ $mode == full ]]; then
  echo 'Disposable worktree checks do not issue an engineering qualification report.' > "$output/engineering-report-not-issued.txt"
fi
