#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-}" in
  --stop) exec docker compose down ;;
  --build|"") ;;
  *) echo 'Usage: ./run.sh [--build|--stop]' >&2; exit 2 ;;
esac
export VIBEARR_WORKER_BASE_IMAGE=${VIBEARR_WORKER_BASE_IMAGE:-vibearr-worker-base:local}
docker build --target worker-base --tag "$VIBEARR_WORKER_BASE_IMAGE" .
VIBEARR_WORKER_BASE_IMAGE_ID=$(docker image inspect "$VIBEARR_WORKER_BASE_IMAGE" --format '{{.Id}}')
export VIBEARR_WORKER_BASE_IMAGE_ID
docker compose build
worker_base_after=$(docker image inspect "$VIBEARR_WORKER_BASE_IMAGE" --format '{{.Id}}') || exit $?
[[ $worker_base_after == "$VIBEARR_WORKER_BASE_IMAGE_ID" ]] || { echo 'Worker toolchain image changed during build.' >&2; exit 1; }
VIBEARR_RUNNER_IMAGE_ID=''
VIBEARR_WORKER_IMAGE_ID=''
image_listing=$(docker compose config --images) || exit $?
while IFS= read -r image; do
  selected=$(docker image inspect "$image" --format '{{index .Config.Labels "io.vibearr.role"}} {{.Id}}' 2>/dev/null) || continue
  case "$selected" in
    'runner '*)
      [[ -z $VIBEARR_RUNNER_IMAGE_ID ]] || { echo 'More than one runner image was selected.' >&2; exit 1; }
      VIBEARR_RUNNER_IMAGE_ID=${selected#runner }
      ;;
    'worker '*)
      [[ -z $VIBEARR_WORKER_IMAGE_ID ]] || { echo 'More than one worker image was selected.' >&2; exit 1; }
      VIBEARR_WORKER_IMAGE_ID=${selected#worker }
      ;;
  esac
done <<< "$image_listing"
[[ -n $VIBEARR_RUNNER_IMAGE_ID ]] || { echo 'Build the registered runner image first.' >&2; exit 1; }
[[ -n $VIBEARR_WORKER_IMAGE_ID ]] || { echo 'Build the registered worker image first.' >&2; exit 1; }
export VIBEARR_RUNNER_IMAGE_ID VIBEARR_WORKER_IMAGE_ID
exec docker compose up -d --wait
