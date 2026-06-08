# tesla-fleet-mqtt

Self-hosted bridge between a Tesla and MQTT, using the official [Tesla Fleet API](https://developer.tesla.com/docs/fleet-api) and [fleet-telemetry](https://github.com/teslamotors/fleet-telemetry). No third-party cloud service.

- Real-time vehicle state streamed to `tesla/<VIN>/v/<field>` (speed, SoC, charging, climate, sentry, doors, GPS, TPMS, ...).
- Commands sent by publishing to `tesla/cmd/<command>` (sentry, climate, charging, locks, ...), signed locally with your own key.
- Runs as a normal Docker Compose stack. Works standalone or alongside an existing reverse proxy. Plays nicely next to TeslaMate/TeslaLogger (a vehicle allows up to 3 telemetry configs).

It is fully env-driven and ships no secrets, so it's safe to keep your fork public and deploy it from git with Dockge, Komodo, Portainer, Dockhand, etc.

## How it works

```
STATE   car --mTLS:443--> [your public IP] --> fleet-telemetry --MQTT--> mosquitto --> your apps
COMMAND your app --MQTT--> mosquitto --> cmd-bridge --HTTP--> http-proxy --signed--> Fleet API --> car
```

- `tesla-fleet-telemetry` receives the car's mutual-TLS stream and publishes each field to MQTT.
- `tesla-http-proxy` (Tesla's vehicle-command SDK) signs commands with your private key — modern cars require signed commands.
- `tesla-cmd-bridge` subscribes to `cmd/#` and forwards to the proxy, managing the OAuth token.
- `tesla-pubkey` serves your public key for the one-time partner registration.
- `mosquitto` is the broker (remove it and set `MQTT_HOST` if you already have one).

## Requirements

- A Docker host reachable from the internet on one port for telemetry (default 443), plus a DNS name for it.
- A second public HTTPS URL to serve a static key file (for one-time registration).
- A Tesla developer account (Fleet API has a metered cost with a $10/month free credit; one car is normally well within it).
- `openssl`, `curl`, `jq`, and `docker` on the host.

## Setup

### 1. Create a Fleet API app
At [developer.tesla.com](https://developer.tesla.com): create an application, add a payment method, and enable scopes **Vehicle Information, Vehicle Location, Vehicle Commands, Vehicle Charging Management** (plus a redirect URI under your domain, e.g. `https://tesla.example.com/auth/callback`). Note the Client ID and Client Secret.

### 2. DNS and ports
- Point `TELEMETRY_HOST` (e.g. `telemetry.example.com`) at your public IP and forward its port (default 443) to the Docker host. This endpoint does its own mutual TLS — do **not** put it behind a TLS-terminating proxy or Cloudflare's orange-cloud (see "Behind a reverse proxy" if 443 is already taken).
- Point `PARTNER_DOMAIN` (e.g. `tesla.example.com`) at something that can serve a static file over **valid public HTTPS** — your existing reverse proxy in front of `tesla-pubkey`, GitHub Pages, anything.

### 3. Configure
```
cp .env.example .env
# edit .env: client id/secret, domains, region, VIN, MQTT password, PUID/PGID
```

### 4. Generate keys and the broker user
```
bash scripts/generate-keys.sh        # fleet key, proxy TLS, telemetry CA + server cert
bash scripts/setup-broker-user.sh    # mosquitto password file for MQTT_USER
```

### 5. Host the public key and register your domain
Serve `pubkey/.well-known/appspecific/com.tesla.3p.public-key.pem` at
`https://<PARTNER_DOMAIN>/.well-known/appspecific/com.tesla.3p.public-key.pem` (start `tesla-pubkey` and put it behind your HTTPS proxy, or copy the file to any HTTPS host). Verify it returns the key, then register:
```
# get a partner token
curl -s -X POST "$TESLA_AUTH_URL" \
  --data-urlencode grant_type=client_credentials \
  --data-urlencode "client_id=$TESLA_CLIENT_ID" \
  --data-urlencode "client_secret=$TESLA_CLIENT_SECRET" \
  --data-urlencode 'scope=openid vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds' \
  --data-urlencode "audience=$FLEET_API_BASE"
# then, with that access_token:
curl -X POST "$FLEET_API_BASE/api/1/partner_accounts" \
  -H "Authorization: Bearer <partner_token>" -H 'Content-Type: application/json' \
  -d '{"domain": "<PARTNER_DOMAIN>"}'
```

### 6. Authorize, get a refresh token, pair the key
Open the authorize URL in a browser logged into your Tesla account (URL-encode the redirect, include every scope, and `prompt_missing_scopes=true`):
```
https://auth.tesla.com/oauth2/v3/authorize?response_type=code&client_id=<CLIENT_ID>&redirect_uri=<ENCODED_REDIRECT>&scope=openid%20offline_access%20vehicle_device_data%20vehicle_location%20vehicle_cmds%20vehicle_charging_cmds&state=x&prompt_missing_scopes=true
```
Approve (tick all, including Vehicle Location), copy the `code` from the redirect, then:
```
bash scripts/get-token.sh    # paste code; confirms "vehicle_location present"; prints refresh token
```
Put the refresh token in `.env` as `TESLA_REFRESH_TOKEN`. Then pair the virtual key on your phone: open `https://tesla.com/_ak/<PARTNER_DOMAIN>` and approve.

### 7. Start the stack and register telemetry
```
docker compose up -d
bash scripts/register-telemetry.sh     # expect updated_vehicles:1
```

### 8. Verify
```
bash scripts/telemetry-status.sh       # state + synced flag
bash scripts/send-cmd.sh flash_lights  # command round-trip
docker exec -it mosquitto mosquitto_sub -h localhost -u tesla -P '<pass>' -t 'tesla/#' -v
```
The car opens the telemetry stream on its next wake/drive; then `tesla/<VIN>/v/...` topics flow.

## MQTT topics

- State: `tesla/<VIN>/v/<field>` (e.g. `Soc`, `VehicleSpeed`, `InsideTemp`, `SentryMode`, `Location`), published retained.
- `tesla/<VIN>/alerts/<name>/current`, `tesla/<VIN>/errors/<name>`, `tesla/<VIN>/connectivity`.
- Commands in: `tesla/cmd/<command>` with a JSON body. Results: `tesla/cmd_result/<command>`.

The stack streams a 19-field default; the full set of ~260 streamable fields is in [`FIELDS.md`](FIELDS.md) — add any to `scripts/register-telemetry.sh` and re-run it.

Examples:
```
bash scripts/send-cmd.sh set_sentry_mode '{"on":true}'
bash scripts/send-cmd.sh auto_conditioning_start
bash scripts/send-cmd.sh charge_start
```

## Behind an existing reverse proxy / shared broker

If you already run a reverse proxy on 443 and/or a broker, use the override in `examples/existing-stack/docker-compose.override.yml`:
```
docker compose -f docker-compose.yml -f examples/existing-stack/docker-compose.override.yml up -d
```
It reuses your existing docker network (so these containers reach your broker and your proxy reaches them), skips the bundled mosquitto, and stops fleet-telemetry from publishing 443. Then:

- Point `MQTT_HOST` at your broker and grant its MQTT user read+write on `<MQTT_TOPIC_BASE>/#` in the broker ACL.
- Route the telemetry hostname through your proxy with TLS **passthrough**, not termination — the car does mutual TLS with fleet-telemetry, so the proxy must not decrypt. See `examples/traefik/tesla-telemetry.yml`.
- Route `PARTNER_DOMAIN` to `tesla-pubkey` (or serve the key file from your existing setup).
- Set `PUID`/`PGID` to match your stack.

## Deploying with Dockhand / Dockge / Komodo

This is a standard git-syncable compose stack, so any of these work:

1. Add a Git stack pointing at this repo, branch `main`, compose file `docker-compose.yml`.
2. Provide config. Simplest: create a `.env` in the stack directory from `.env.example` — Compose reads it natively, the helper scripts read the same file, and it's gitignored so syncs won't clobber it. (You can also use the tool's env panel; Dockhand keeps those in a separate `.env.dockhand`.)
3. Generate keys on the host in the stack directory — `bash scripts/generate-keys.sh` and `bash scripts/setup-broker-user.sh`. These need a host shell (openssl), and the generated `keys/ proxy/ certs/ pubkey/` are gitignored, so git sync preserves them.
4. Existing reverse proxy or shared broker? Copy `examples/existing-stack/docker-compose.override.yml` to the stack root (Compose and these tools auto-merge `docker-compose.override.yml`) and set the matching env (`MQTT_HOST`, `PUID`/`PGID`, `TELEMETRY_BIND`).
5. Deploy, then run the helper scripts (see below).

### Running the helper scripts securely

If your tool keeps secrets out of files on disk (e.g. Dockhand secret vars), don't run the scripts on the host — they wouldn't see the secrets, and reading them via `docker inspect` is leaky. Instead enable the `tools` profile and run the scripts inside the `tesla-tools` container, which Dockhand injects the secrets into directly:
```
COMPOSE_PROFILES=history,tools          # in your env
docker exec tesla-tools bash scripts/register-telemetry.sh
docker exec tesla-tools bash scripts/telemetry-status.sh
docker exec -it tesla-tools bash scripts/send-cmd.sh flash_lights
```
No secret file, no `docker inspect`. The container reaches the proxy/broker/InfluxDB/MariaDB by name on the shared network. Standalone (non-Dockhand) users with a full `.env` can still run the scripts on the host.

If the tool runs compose from an internal path that differs from the host path (Dockhand stores stacks under `/app/data/...`, which the host daemon can't resolve), bind mounts land on empty dirs and the proxy logs `open /keys/fleet-key.pem: no such file`. Fix: set `STACK_DIR` to the absolute **host** path of the stack directory (e.g. `/mnt/.../appdata/dockhand/stacks/<host>/<stack>`), so binds use that instead of relative paths.

## Troubleshooting (lessons learned)

- "This endpoint must be called through the Vehicle Command HTTP Proxy" — `fleet_telemetry_config` and commands are signed; `register-telemetry.sh` already routes through the proxy.
- `missing_key` when registering — the car needs the virtual key paired (`tesla.com/_ak/<PARTNER_DOMAIN>`) and to be awake. Authorizing the app to your account is a separate step from pairing the key.
- `synced: false` — normal right after registering. The car applies the config on its next wake/drive, not while asleep. A short drive is the reliable trigger.
- `Unauthorized missing scopes vehicle_location` — your token lacks the location scope. Re-authorize **with `prompt_missing_scopes=true`** — Tesla reuses an existing consent and silently ignores added scopes without it. Verify with `get-token.sh`, which decodes the token's `scp` claim (Tesla doesn't return `scope` in the token response).
- `permission denied` reading the key in the proxy/telemetry logs — the key files must be readable by the container user. Containers run as `PUID:PGID`; `generate-keys.sh` chmods them 644.
- No state but command works — check it's not a broker ACL (if your broker restricts topics, grant the MQTT user `tesla/#`), and confirm the car actually connected: `docker logs tesla-fleet-telemetry` shows `socket_connected ... vehicle_device`.
- `set_sentry_mode` reports 200 but the app still shows on — the app caches; trust `tesla/<VIN>/v/SentryMode`.

## Credits

Built on Tesla's [fleet-telemetry](https://github.com/teslamotors/fleet-telemetry) and [vehicle-command](https://github.com/teslamotors/vehicle-command). MIT licensed — see `LICENSE`.
