#!/usr/bin/env bash

# This script is intended to serve as the standard for how we test things before merging any branches for `nightly-queue`
# Typical usage is simply `bash test.sh`
# If you supply any args, they are passed directly to `manage.py test` to run specific tests, e.g. `bash test.sh hello.tests`

# The following lines disable some shellcheck rules that are not applicable to this specific script.
# shellcheck disable=SC2086
# shellcheck disable=SC2048

COMPOSE_FILE="docker-compose.yml"
# These are the compose SERVICE names (not container names) so that
# `docker compose exec <service>` works.
DJANGO_SERVICE="nightly-queue"
WORKER_SERVICE="celery-worker"

# Just fancy versions of echo
function info() { echo -e "\033[1;33m[*] ${*}\033[0m"; }           # Yellow
function warn() { echo -e "\033[1;31m[!] WARN: ${*}\033[0m" >&2; } # Red
function error() { echo -e "\033[1;31m[-] ERROR: ${*}\033[0m" >&2 ; exit 1 ; } # Red

function pass() { echo -e "\033[1;32m[+] PASS: ${*}\033[0m"; } # Green
function fail() { echo -e "\033[1;31m[-] FAIL: ${*}\033[0m" >&2 ; exit 1 ; }                                                                 # Red
function testing() { echo -e "\033[1;34m[?] TEST: ${*}\033[0m"; } # Blue


function docker_exec() {
  local service_name
  service_name=$1
  shift
  docker compose -f $COMPOSE_FILE exec -T "$service_name" $*
}

function test_command() {
  local command
  local label
  command="$1"
  label="$1"

  if test -n "$2"; then
    label="$2"
  fi

  testing "$label" &&
    docker_exec "$DJANGO_SERVICE" $command &&
    pass "$label" ||
    fail "$label"
}

function wait_for_django() {
  info "Waiting for django to be ready..."
  for i in $(seq 1 60); do
    if docker compose -f $COMPOSE_FILE exec -T "$DJANGO_SERVICE" \
       curl -s localhost:8000 | grep -q "Your queue"; then
      pass "django is ready"
      return 0
    fi
    echo -n "."
    sleep 2
  done
  echo
  warn "django did not respond within 120s; continuing anyway"
  return 0
}

function has_args() {
  if [ "$#" -gt 0 ]; then
    return 0
  else
    return 1

  fi

}

function run_lints() {
  info "Running lint checks..." &&
    test_command "black --check ." "black format check" &&
    test_command "ruff check ." "ruff lint check" &&
    test_command "mypy . --config-file mypy.ini" "mypy type check" &&
    pass "All linting checks passed!!!" ||
    fail "Lint failed :("
}

function run_tests() {
  info "Running tests..." &&
    test_command "python manage.py makemigrations --check --dry-run" "no pending migrations" &&
    test_command "python manage.py migrate --noinput --check" "database migrations applied" &&
    test_command "python manage.py check" "django system check" &&
    test_command "python manage.py test" "unit + celery e2e tests" &&
    pass "All tests passed!!!" ||
    fail "One or more tests failed. :("
}

function Main() {
  cd "$(dirname "$0")" || error "Failed to change directory to $(dirname "$0")"

  info "Bringing up container..." &&
    docker compose -f $COMPOSE_FILE build &&
    docker compose -f $COMPOSE_FILE up -d --wait &&
    pass "All containers are up" ||
    fail "Could not start containers :("

  wait_for_django

  if has_args "$@"; then
    info "Running unit tests only..." &&
      test_command "python manage.py test $*" "unit tests"
  fi

  if ! has_args "$@"; then
    run_lints
    echo
    run_tests
  fi

}

Main "$@"
