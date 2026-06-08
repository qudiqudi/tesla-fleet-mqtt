#!/usr/bin/env bash
# Diagnose the MQTT pipeline: broker round-trip, command path, state stream.
# Uses the bundled mosquitto container.  bash scripts/mqtt-test.sh
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need MQTT_USER MQTT_PASSWORD MQTT_TOPIC_BASE TESLA_VIN
BASE="${MQTT_TOPIC_BASE%/}"

MQ_HOST="${MQTT_HOST:-mosquitto}"; MQ_PORT="${MQTT_PORT:-1883}"
pub(){ mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }
sub(){ mosquitto_sub -h "$MQ_HOST" -p "$MQ_PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }

echo "=== 1. broker round-trip ==="
pub -t "$BASE/diag" -r -m 'hello' 2>&1
OUT=$(sub -t "$BASE/diag" -C 1 -W 4 -v 2>&1)
pub -t "$BASE/diag" -r -n 2>/dev/null
echo "$OUT" | grep -q "^$BASE/diag hello" && echo "  OK -> $OUT" || echo "  FAIL ($OUT) — broker auth/ACL problem for '$MQTT_USER'"

echo
echo "=== 2. command path: set_sentry_mode on (result on $BASE/cmd_result) ==="
sub -t "$BASE/cmd_result/set_sentry_mode" -C 1 -W 12 -v &
SP=$!
sleep 1
pub -t "$BASE/cmd/set_sentry_mode" -m '{"on":true}'
wait $SP || echo "  (no result within 12s)"

echo
echo "=== 3. state stream: tesla/$TESLA_VIN/# for 20s ==="
sub -t "$BASE/$TESLA_VIN/#" -v -W 20 2>&1 | head -40
echo "=== done ==="
