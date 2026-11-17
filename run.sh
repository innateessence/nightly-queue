#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-}" in
  --stop) exec docker compose down ;;
  --build|"") docker compose build ;;
  *) echo 'Usage: ./run.sh [--build|--stop]' >&2; exit 2 ;;
esac
VIBEARR_RUNNER_IMAGE_ID=''
while IFS= read -r image; do
  selected=$(docker image inspect "$image" --format '{{if eq (index .Config.Labels "io.vibearr.role") "runner"}}{{.Id}}{{end}}' 2>/dev/null || true)
  if [[ -n $selected ]]; then
    [[ -z $VIBEARR_RUNNER_IMAGE_ID ]] || { echo 'More than one runner image was selected.' >&2; exit 1; }
    VIBEARR_RUNNER_IMAGE_ID=$selected
  fi
done < <(docker compose config --images)
[[ -n $VIBEARR_RUNNER_IMAGE_ID ]] || { echo 'Build the registered runner image first.' >&2; exit 1; }
export VIBEARR_RUNNER_IMAGE_ID
exec docker compose up -d --wait
