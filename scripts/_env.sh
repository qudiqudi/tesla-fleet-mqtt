#!/usr/bin/env bash
# Sourced by the other scripts. Loads config without keeping secrets on disk:
#   1. .env.dockhand (Dockhand's generated non-secret vars) and/or a local .env (standalone).
#   2. Any Dockhand-managed secret still unset is pulled from the running container's env,
#      where Dockhand injected it at runtime — so no plaintext secret file is needed.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

loaded=0
for f in "$ROOT/.env.dockhand" "$ROOT/.env"; do
  if [ -f "$f" ]; then set -a; . "$f"; set +a; loaded=1; fi
done

# Pull a var from a container's runtime env if not already set (tries non-sudo, then sudo).
_pull() {
  [ -n "${!1:-}" ] && return 0
  local v
  v=$(docker inspect "$2" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n "s/^$1=//p" | head -1)
  [ -z "$v" ] && v=$(sudo docker inspect "$2" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null | sed -n "s/^$1=//p" | head -1)
  [ -n "$v" ] && export "$1=$v"
}
_pull TESLA_CLIENT_SECRET tesla-cmd-bridge
_pull TESLA_REFRESH_TOKEN tesla-cmd-bridge
_pull MQTT_PASSWORD      tesla-cmd-bridge
_pull DB_PASSWORD        tesla-sessionizer
_pull INFLUX_TOKEN       tesla-influx

if [ "$loaded" != 1 ]; then
  echo "No .env or .env.dockhand at $ROOT — copy .env.example to .env (or set vars in Dockhand)." >&2
  exit 1
fi

need() { for v in "$@"; do [ -n "${!v:-}" ] || { echo "Missing $v (not in env file or any container)" >&2; exit 1; }; done; }
