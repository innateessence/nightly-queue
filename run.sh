#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-}" in
  --stop) docker compose down ;;
  --build) docker compose build && docker compose up -d --wait ;;
  "") docker compose up -d --build --wait ;;
  *) echo 'Usage: ./run.sh [--build|--stop]' >&2; exit 2 ;;
esac
