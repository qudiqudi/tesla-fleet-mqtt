#!/usr/bin/env bash
# Create the mosquitto password file for MQTT_USER/MQTT_PASSWORD from .env.
# Run once before first start:  bash scripts/setup-broker-user.sh
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need MQTT_USER MQTT_PASSWORD

cd "$ROOT"
mkdir -p mosquitto/config
docker run --rm -v "$ROOT/mosquitto/config:/mosquitto/config" eclipse-mosquitto:2 \
  mosquitto_passwd -b -c /mosquitto/config/passwd "$MQTT_USER" "$MQTT_PASSWORD"
chmod 644 mosquitto/config/passwd
echo "Wrote mosquitto/config/passwd for user '$MQTT_USER'."
