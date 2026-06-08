#!/usr/bin/env bash
# Sourced by the other scripts: loads .env from the repo root into the environment.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Prefer a local .env; fall back to Dockhand's generated .env.dockhand.
ENVF=""
for c in "$ROOT/.env" "$ROOT/.env.dockhand"; do [ -f "$c" ] && ENVF="$c" && break; done
if [ -z "$ENVF" ]; then
  echo "No .env or .env.dockhand at $ROOT — copy .env.example to .env (or set vars in Dockhand)." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
. "$ENVF"
set +a
need(){ for v in "$@"; do [ -n "${!v:-}" ] || { echo "Missing $v in .env" >&2; exit 1; }; done; }
