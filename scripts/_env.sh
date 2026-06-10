#!/usr/bin/env bash
# Sourced by the other scripts. Intended to run inside the tesla-tools container, where
# Dockhand injects all vars (incl. secrets) into the process env directly — no secret file,
# no docker inspect. Standalone users can instead provide a local .env.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# If a local env file exists (standalone use), load it. In the tools container there is none;
# the variables are already present in the environment.
for f in "$ROOT/.env.dockhand" "$ROOT/.env"; do
  if [ -f "$f" ]; then set -a; . "$f"; set +a; fi
done

need() { for v in "$@"; do [ -n "${!v:-}" ] || { echo "Missing $v in environment" >&2; exit 1; }; done; }

# Broker credential wiring for the diagnostic/command scripts — one copy here so TLS/port/auth
# changes apply to every script (callers: need MQTT_USER MQTT_PASSWORD first).
MQ_HOST="${MQTT_HOST:-mosquitto}"; MQ_PORT="${MQTT_PORT:-1883}"
pub(){ mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }
sub(){ mosquitto_sub -h "$MQ_HOST" -p "$MQ_PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }

# Return a Fleet API access token. PREFER the one tesla-cmd-bridge maintains (shared via a
# read-only volume), so the helper scripts don't run their own refresh_token grant — Tesla
# rotates the refresh token and independent refreshers fork the lineage, invalidating the
# bridge's token (which breaks commands). Fall back to a direct refresh only when the shared
# token isn't available (standalone use without the bridge).
access_token() {
  local f="${BRIDGE_ACCESS_TOKEN_FILE:-/bridge-data/access_token}" t
  if [ -r "$f" ]; then
    t="$(cat "$f" 2>/dev/null)"
    if [ -n "$t" ]; then printf '%s' "$t"; return 0; fi
  fi
  # In automated contexts (the on-deploy registrar) we must NOT refresh directly — that would
  # fork the refresh-token lineage. Require the shared token instead of falling back.
  if [ -n "${ACCESS_TOKEN_REQUIRED:-}" ]; then
    echo "access_token: required shared bridge token missing at $f (refusing to fork the lineage)" >&2
    return 1
  fi
  echo "access_token: no shared bridge token at $f -> refreshing directly (standalone; this rotates the token)" >&2
  need TESLA_CLIENT_ID TESLA_REFRESH_TOKEN TESLA_AUTH_URL
  curl -s "$TESLA_AUTH_URL" \
    --data-urlencode grant_type=refresh_token \
    --data-urlencode "client_id=$TESLA_CLIENT_ID" \
    --data-urlencode "client_secret=${TESLA_CLIENT_SECRET:-}" \
    --data-urlencode "refresh_token=$TESLA_REFRESH_TOKEN" | jq -r '.access_token // empty'
}
