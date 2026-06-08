#!/usr/bin/env bash
# Diagnose the MQTT pipeline: broker round-trip, command path, state stream.
# Uses the bundled mosquitto container.  bash scripts/mqtt-test.sh
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need MQTT_USER MQTT_PASSWORD MQTT_TOPIC_BASE TESLA_VIN
BASE="${MQTT_TOPIC_BASE%/}"

pub(){ docker exec mosquitto mosquitto_pub -h localhost -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }
sub(){ docker exec mosquitto mosquitto_sub -h localhost -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }

echo "=== 1. broker round-trip ==="
pub -t "$BASE/diag" -r -m 'hello' 2>&1
OUT=$(sub -t "$BASE/diag" -C 1 -W 4 -v 2>&1)
pub -t "$BASE/diag" -r -n 2>/dev/null
echo "$OUT" | grep -q "^$BASE/diag hello" && echo "  OK -> $OUT" || echo "  FAIL ($OUT) — broker auth/ACL problem for '$MQTT_USER'"

echo
echo "=== 2. command path: set_sentry_mode on ==="
pub -t "$BASE/cmd/set_sentry_mode" -m '{"on":true}'
sleep 6
docker logs tesla-cmd-bridge --since 30s 2>&1 | grep -E 'cmd |auth:|mqtt:' | tail -6 || echo "  (no bridge log lines)"

echo
echo "=== 3. state stream: tesla/$TESLA_VIN/# for 20s ==="
sub -t "$BASE/$TESLA_VIN/#" -v -W 20 2>&1 | head -40
echo "=== done ==="
