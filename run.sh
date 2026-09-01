#!/usr/bin/env bash
# Build and run the nightly-queue Django app via docker-compose.
# Usage:
#   ./run.sh            # build (if needed) + start the container
#   ./run.sh --build    # force rebuild the image first
#   ./run.sh --stop     # stop and remove the container
#   PORT=8080 ./run.sh  # expose on a different host port (default 8000)
set -euo pipefail

# shellcheck disable=SC2086
# shellcheck disable=SC2048

COMPOSE_FILE="docker-compose.yml"

info() { echo -e "\033[1;33m[*] ${*}\033[0m"; }
error() { echo -e "\033[1;31m[-] ERROR: ${*}\033[0m" >&2 ; exit 1 ; }

REBUILD=0
ACTION="run"

for arg in "$@"; do
    case "$arg" in
        --build) REBUILD=1 ;;
        --stop)  ACTION="stop" ;;
        -h|--help)
            grep '^# ' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) error "unknown flag: $arg (try --help)" ;;
    esac
done

cd "$(dirname "$0")" || error "Failed to change directory to $(dirname "$0")"

if ! docker compose -f "$COMPOSE_FILE" config >/dev/null 2>&1; then
    error "docker compose is not available or $COMPOSE_FILE is invalid"
fi

if [[ "${ACTION}" == "stop" ]]; then
    info "Stopping container..."
    docker compose -f "$COMPOSE_FILE" down
    echo "==> stopped"
    exit 0
fi

if [[ "${REBUILD}" -eq 1 ]] || ! docker image inspect "nightly-queue-django" >/dev/null 2>&1; then
    info "Building image..."
    docker compose -f "$COMPOSE_FILE" build
else
    info "Reusing existing image nightly-queue-django (use --build to force rebuild)"
fi

info "Starting container..."
docker compose -f "$COMPOSE_FILE" up -d --wait
echo
echo "    app:      http://127.0.0.1:${PORT:-8000}/"
echo "    admin:    http://127.0.0.1:${PORT:-8000}/admin/"
echo
echo "    logs:     docker compose -f ${COMPOSE_FILE} logs -f"
echo "    shell:    docker compose -f ${COMPOSE_FILE} exec nightly-queue sh"
echo "    stop:     ./run.sh --stop"
