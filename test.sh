#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if (($#)); then
  exec bash deploy/docker/checks.sh unit -- "$@"
fi
exec bash deploy/docker/checks.sh full
