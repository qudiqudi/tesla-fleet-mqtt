#!/usr/bin/env bash
# Sourced by the other scripts: loads .env from the repo root into the environment.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -f "$ROOT/.env" ]; then
  echo "No .env found at $ROOT/.env — copy .env.example to .env and fill it in." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "$ROOT/.env"
set +a
need(){ for v in "$@"; do [ -n "${!v:-}" ] || { echo "Missing $v in .env" >&2; exit 1; }; done; }
