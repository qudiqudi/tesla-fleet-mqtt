#!/usr/bin/env bash
# Register the fleet telemetry streaming config for your vehicle, via the command proxy
# (fleet_telemetry_config is a signed endpoint, so it must go through tesla-http-proxy).
# Requires the stack (at least tesla-http-proxy) to be running.  bash scripts/register-telemetry.sh
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need TESLA_CLIENT_ID TESLA_CLIENT_SECRET TESLA_REFRESH_TOKEN TESLA_VIN TELEMETRY_HOST TESLA_AUTH_URL
command -v jq >/dev/null || { echo "jq is required"; exit 1; }

PROXY_CERT="$ROOT/proxy/tls-cert.pem"
CA_CRT="$ROOT/certs/ca.crt"
PROXY_URL="${PROXY_URL:-https://tesla-http-proxy:4443}"
TELEMETRY_PORT="${TELEMETRY_PORT:-443}"
[ -r "$PROXY_CERT" ] || { echo "Missing $PROXY_CERT (run generate-keys.sh and start the proxy)"; exit 1; }
[ -r "$CA_CRT" ]    || { echo "Missing $CA_CRT (run generate-keys.sh)"; exit 1; }

echo "Requesting access token..."
ACCESS=$(curl -s "$TESLA_AUTH_URL" \
  --data-urlencode grant_type=refresh_token \
  --data-urlencode "client_id=$TESLA_CLIENT_ID" \
  --data-urlencode "client_secret=$TESLA_CLIENT_SECRET" \
  --data-urlencode "refresh_token=$TESLA_REFRESH_TOKEN" | jq -r '.access_token // empty')
[ -z "$ACCESS" ] && { echo "failed to get access token"; exit 1; }

# Field list + intervals (seconds). Location needs the vehicle_location scope.
# Full list of available fields: ../FIELDS.md
jq -n --arg vin "$TESLA_VIN" --arg ca "$(cat "$CA_CRT")" \
  --arg host "$TELEMETRY_HOST" --argjson port "$TELEMETRY_PORT" '{
  vins: [$vin],
  config: {
    hostname: $host,
    port: $port,
    ca: $ca,
    fields: {
      VehicleSpeed:    {interval_seconds: 10},
      Soc:             {interval_seconds: 60},
      BatteryLevel:    {interval_seconds: 60},
      ChargeState:     {interval_seconds: 60},
      ACChargingPower:    {interval_seconds: 30},
      DCChargingPower:    {interval_seconds: 30},
      ACChargingEnergyIn: {interval_seconds: 60},
      DCChargingEnergyIn: {interval_seconds: 60},
      RatedRange:         {interval_seconds: 120},
      IdealBatteryRange:  {interval_seconds: 60},
      EstBatteryRange:    {interval_seconds: 60},
      EnergyRemaining:    {interval_seconds: 60},
      PackVoltage:        {interval_seconds: 10},
      PackCurrent:        {interval_seconds: 10},
      ChargerVoltage:     {interval_seconds: 30},
      Location:        {interval_seconds: 10},
      Gear:            {interval_seconds: 5},
      Odometer:        {interval_seconds: 300},
      InsideTemp:      {interval_seconds: 60},
      OutsideTemp:     {interval_seconds: 60},
      HvacPower:       {interval_seconds: 30},
      SentryMode:      {interval_seconds: 30},
      DoorState:       {interval_seconds: 5},
      Locked:          {interval_seconds: 10},
      TpmsPressureFl:  {interval_seconds: 300},
      TpmsPressureFr:  {interval_seconds: 300},
      TpmsPressureRl:  {interval_seconds: 300},
      TpmsPressureRr:  {interval_seconds: 300}
    }
  }
}' > /tmp/tesla-tcfg.json

echo "Registering via $PROXY_URL ..."
curl -s --request POST "$PROXY_URL/api/1/vehicles/fleet_telemetry_config" \
  --cacert "$PROXY_CERT" \
  --header "Authorization: Bearer $ACCESS" \
  --header 'Content-Type: application/json' \
  --data @/tmp/tesla-tcfg.json | jq .
rm -f /tmp/tesla-tcfg.json
echo "Expect updated_vehicles:1. If skipped_vehicles.missing_key: pair the virtual key and wake the car."
