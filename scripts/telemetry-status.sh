#!/usr/bin/env bash
# Show vehicle online state and whether the telemetry config is synced to the car.
# bash scripts/telemetry-status.sh
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need TESLA_CLIENT_ID TESLA_CLIENT_SECRET TESLA_REFRESH_TOKEN TESLA_VIN FLEET_API_BASE TESLA_AUTH_URL
command -v jq >/dev/null || { echo "jq is required"; exit 1; }

ACCESS=$(curl -s "$TESLA_AUTH_URL" \
  --data-urlencode grant_type=refresh_token \
  --data-urlencode "client_id=$TESLA_CLIENT_ID" \
  --data-urlencode "client_secret=$TESLA_CLIENT_SECRET" \
  --data-urlencode "refresh_token=$TESLA_REFRESH_TOKEN" | jq -r '.access_token // empty')
[ -z "$ACCESS" ] && { echo "no access token"; exit 1; }
AUTH="Authorization: Bearer $ACCESS"

echo "=== vehicle online state ==="
curl -s "$FLEET_API_BASE/api/1/vehicles/$TESLA_VIN" -H "$AUTH" | jq '.response | {state, in_service, api_version}' 2>/dev/null \
  || curl -s "$FLEET_API_BASE/api/1/vehicles/$TESLA_VIN" -H "$AUTH"

echo
echo "=== fleet_telemetry_config (synced to car?) ==="
RESP=$(curl -s "$FLEET_API_BASE/api/1/vehicles/$TESLA_VIN/fleet_telemetry_config" -H "$AUTH")
echo "$RESP" | jq '.response | {synced, hostname: .config.hostname, port: .config.port, fields: (.config.fields|keys)}' 2>/dev/null || echo "$RESP"
echo
echo "Note: synced flips to true on the car's next wake/drive, not while asleep."
