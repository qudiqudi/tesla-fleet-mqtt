#!/usr/bin/env bash
# Sourced by the other scripts: loads .env from the repo root into the environment.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Load env, layered: Dockhand's generated .env.dockhand (non-secret vars) first, then a
# local .env on top. Under Dockhand, secrets aren't written to disk, so keep them in a small
# host-only .env (gitignored) — e.g. TESLA_CLIENT_SECRET, TESLA_REFRESH_TOKEN, MQTT_PASSWORD,
# INFLUX_TOKEN, DB_PASSWORD. Standalone users just use a full .env.
loaded=0
for f in "$ROOT/.env.dockhand" "$ROOT/.env"; do
  if [ -f "$f" ]; then set -a; . "$f"; set +a; loaded=1; fi
done
if [ "$loaded" != 1 ]; then
  echo "No .env or .env.dockhand at $ROOT — copy .env.example to .env (or set vars in Dockhand)." >&2
  exit 1
fi
need(){ for v in "$@"; do [ -n "${!v:-}" ] || { echo "Missing $v in .env" >&2; exit 1; }; done; }
