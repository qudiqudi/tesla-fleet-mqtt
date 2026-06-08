#!/usr/bin/env bash
# Send a Tesla command over MQTT and print the car's response.
# Uses the bundled mosquitto container. Usage:
#   bash scripts/send-cmd.sh flash_lights
#   bash scripts/send-cmd.sh honk_horn
#   bash scripts/send-cmd.sh auto_conditioning_start
#   bash scripts/send-cmd.sh set_sentry_mode '{"on":true}'
#   bash scripts/send-cmd.sh set_sentry_mode '{"on":false}'
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need MQTT_USER MQTT_PASSWORD MQTT_TOPIC_BASE

CMD="${1:-flash_lights}"
BODY='{}'
[ "$#" -ge 2 ] && BODY="$2"
BASE="${MQTT_TOPIC_BASE%/}"

MQ_HOST="${MQTT_HOST:-mosquitto}"; MQ_PORT="${MQTT_PORT:-1883}"
pub(){ mosquitto_pub -h "$MQ_HOST" -p "$MQ_PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }
sub(){ mosquitto_sub -h "$MQ_HOST" -p "$MQ_PORT" -u "$MQTT_USER" -P "$MQTT_PASSWORD" "$@"; }

echo "-> $BASE/cmd/$CMD   body=$BODY"
sub -t "$BASE/cmd_result/$CMD" -C 1 -W 20 -v &
SUBPID=$!
sleep 1
pub -t "$BASE/cmd/$CMD" -m "$BODY"
echo "   waiting up to 20s for $BASE/cmd_result/$CMD ..."
wait $SUBPID
