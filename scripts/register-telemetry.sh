#!/usr/bin/env bash
# Register the fleet telemetry streaming config for your vehicle, via the command proxy
# (fleet_telemetry_config is a signed endpoint, so it must go through tesla-http-proxy).
# Requires the stack (at least tesla-http-proxy) to be running.  bash scripts/register-telemetry.sh
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need TESLA_VIN TELEMETRY_HOST TESLA_AUTH_URL
command -v jq >/dev/null || { echo "jq is required"; exit 1; }

PROXY_CERT="$ROOT/proxy/tls-cert.pem"
CA_CRT="$ROOT/certs/ca.crt"
PROXY_URL="${PROXY_URL:-https://tesla-http-proxy:4443}"
TELEMETRY_PORT="${TELEMETRY_PORT:-443}"
# GPS / speed streaming cadence (seconds). 1 = densest tracks (GPS updates ~1/s on the car);
# raise to 2-3 for teslalogger-parity with less write volume. Keep tlwriter POS_DRIVE_INTERVAL in sync.
LOCATION_INTERVAL="${LOCATION_INTERVAL:-1}"
SPEED_INTERVAL="${SPEED_INTERVAL:-1}"
[ -r "$PROXY_CERT" ] || { echo "Missing $PROXY_CERT (run generate-keys.sh and start the proxy)"; exit 1; }
[ -r "$CA_CRT" ]    || { echo "Missing $CA_CRT (run generate-keys.sh)"; exit 1; }

echo "Requesting access token..."
ACCESS=$(access_token)
[ -z "$ACCESS" ] && { echo "failed to get access token"; exit 1; }

# Field list + intervals (seconds). Location needs the vehicle_location scope.
# Full list of available fields: ../FIELDS.md
jq -n --arg vin "$TESLA_VIN" --arg ca "$(cat "$CA_CRT")" \
  --arg host "$TELEMETRY_HOST" --argjson port "$TELEMETRY_PORT" \
  --argjson loci "$LOCATION_INTERVAL" --argjson speedi "$SPEED_INTERVAL" '{
  vins: [$vin],
  config: {
    hostname: $host,
    port: $port,
    ca: $ca,
    fields: {
      VehicleSpeed:    {interval_seconds: $speedi},
      Soc:             {interval_seconds: 10},
      BatteryLevel:    {interval_seconds: 10},
      ChargeState:     {interval_seconds: 30},
      DetailedChargeState: {interval_seconds: 30},
      ChargeLimitSoc:      {interval_seconds: 60},
      ChargeCurrentRequest: {interval_seconds: 60},
      ChargeRateMilePerHour: {interval_seconds: 10},
      ChargingCableType:  {interval_seconds: 60},
      ChargePortDoorOpen: {interval_seconds: 30},
      BatteryHeaterOn:    {interval_seconds: 60},
      ACChargingPower:    {interval_seconds: 5},
      DCChargingPower:    {interval_seconds: 5},
      ACChargingEnergyIn: {interval_seconds: 10},
      DCChargingEnergyIn: {interval_seconds: 10},
      RatedRange:         {interval_seconds: 120},
      IdealBatteryRange:  {interval_seconds: 60},
      EstBatteryRange:    {interval_seconds: 60},
      EnergyRemaining:    {interval_seconds: 30},
      PackVoltage:        {interval_seconds: 1},
      PackCurrent:        {interval_seconds: 1},
      ChargerVoltage:     {interval_seconds: 5},
      Location:        {interval_seconds: $loci},
      Gear:            {interval_seconds: 1},
      Odometer:        {interval_seconds: 1},
      InsideTemp:      {interval_seconds: 60},
      OutsideTemp:     {interval_seconds: 60},
      HvacPower:       {interval_seconds: 10},
      SentryMode:      {interval_seconds: 30},
      DoorState:       {interval_seconds: 5},
      Locked:          {interval_seconds: 10},
      SettingDistanceUnit: {interval_seconds: 30},
      TpmsPressureFl:  {interval_seconds: 300},
      TpmsPressureFr:  {interval_seconds: 300},
      TpmsPressureRl:  {interval_seconds: 300},
      TpmsPressureRr:  {interval_seconds: 300},
      Version:             {interval_seconds: 600},
      SoftwareUpdateVersion: {interval_seconds: 600},
      SoftwareUpdateDownloadPercentComplete: {interval_seconds: 60},
      SoftwareUpdateInstallationPercentComplete: {interval_seconds: 60},
      DestinationName:     {interval_seconds: 30},
      DestinationLocation: {interval_seconds: 30},
      MinutesToArrival:    {interval_seconds: 30},
      RouteTrafficMinutesDelay: {interval_seconds: 60},
      ExpectedEnergyPercentAtTripArrival: {interval_seconds: 60},
      GpsHeading:          {interval_seconds: 5},
      TimeToFullCharge:    {interval_seconds: 60},
      FastChargerType:     {interval_seconds: 60},
      FastChargerBrand:    {interval_seconds: 60},
      FdWindow:            {interval_seconds: 30},
      FpWindow:            {interval_seconds: 30},
      RdWindow:            {interval_seconds: 30},
      RpWindow:            {interval_seconds: 30},
      ModuleTempMin:       {interval_seconds: 60},
      ModuleTempMax:       {interval_seconds: 60}
    }
  }
}' > /tmp/tesla-tcfg.json

# This runs unattended on every deploy (the tesla-register service), so the exit code is the
# only signal: a down proxy or a Tesla 4xx/5xx must NOT end in a green "registered" deploy.
echo "Registering via $PROXY_URL ..."
RESP=/tmp/tesla-tcfg-resp.json
HTTP_CODE=$(curl -s -o "$RESP" -w '%{http_code}' --request POST "$PROXY_URL/api/1/vehicles/fleet_telemetry_config" \
  --cacert "$PROXY_CERT" \
  --header "Authorization: Bearer $ACCESS" \
  --header 'Content-Type: application/json' \
  --data @/tmp/tesla-tcfg.json) || { echo "registration request failed (proxy unreachable?)"; rm -f /tmp/tesla-tcfg.json "$RESP"; exit 1; }
rm -f /tmp/tesla-tcfg.json
jq . "$RESP" 2>/dev/null || cat "$RESP"
if [ "$HTTP_CODE" -lt 200 ] || [ "$HTTP_CODE" -ge 300 ]; then
  echo "registration FAILED: HTTP $HTTP_CODE"; rm -f "$RESP"; exit 1
fi
if ! jq -e '(.response.updated_vehicles // .updated_vehicles) == 1' "$RESP" >/dev/null 2>&1; then
  echo "registration NOT applied (updated_vehicles != 1). If skipped_vehicles.missing_key: pair the virtual key and wake the car."
  rm -f "$RESP"; exit 1
fi
rm -f "$RESP"
echo "Telemetry config registered (updated_vehicles: 1)."
