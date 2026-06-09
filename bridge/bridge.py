#!/usr/bin/env python3
"""
tesla-cmd-bridge: subscribe to <base>/cmd/# on MQTT and forward each message
as a signed Fleet API command via tesla-http-proxy.

Topic:   <base>/cmd/<command>      e.g. tesla/cmd/set_sentry_mode
Payload: JSON body for the command, or empty for none.
         {"on": true}              -> tesla/cmd/set_sentry_mode
         {}                        -> tesla/cmd/auto_conditioning_start
Result:  published to <base>/cmd_result/<command> as {"status": <int>, "resp": <str>}

Some controls are naturally on/off in Home Assistant but Fleet exposes them as paired
start/stop endpoints. TOGGLE_ALIASES maps a single "<alias>" command carrying {"on": bool}
to the right endpoint, so a plain HA switch (one command_topic, payload_on/off) works:
         {"on": true}  -> tesla/cmd/charge -> charge_start ; {"on": false} -> charge_stop

The refresh token is seeded from the environment; Tesla rotates it on every refresh,
so the bridge captures the new refresh token from each response and persists it to
REFRESH_TOKEN_FILE (a private volume), loading that on startup in preference to the env
value. Without this, recreating the container (e.g. a redeploy) falls back to the stale
env token and 401s.
"""
import json
import os
import sys
import threading
import time

import requests
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "tesla")
MQTT_PASS = os.environ["MQTT_PASSWORD"]
BASE = os.environ.get("MQTT_TOPIC_BASE", "tesla").rstrip("/")
CMD_TOPIC = f"{BASE}/cmd/#"

PROXY_URL = os.environ.get("PROXY_URL", "https://tesla-http-proxy:4443").rstrip("/")
PROXY_CACERT = os.environ.get("PROXY_CACERT", "/certs/proxy-ca.pem")

VIN = os.environ["TESLA_VIN"]
CLIENT_ID = os.environ["TESLA_CLIENT_ID"]
CLIENT_SECRET = os.environ.get("TESLA_CLIENT_SECRET", "")
ENV_REFRESH_TOKEN = os.environ["TESLA_REFRESH_TOKEN"]   # the seed from .env (re-auth updates this)
REFRESH_TOKEN = ENV_REFRESH_TOKEN                       # current working token; persisted one wins if present
AUTH_URL = os.environ.get("TESLA_AUTH_URL", "https://auth.tesla.com/oauth2/v3/token")
TOKEN_FILE = os.environ.get("REFRESH_TOKEN_FILE", "/data/refresh_token")
# The bridge is the SINGLE owner of the rotating refresh-token lineage. It writes the current
# access token here so the helper scripts (register-telemetry, telemetry-status) can use it
# instead of running their own refresh_token grant — independent refreshers fork the lineage
# and Tesla invalidates all but the newest, which is what kept killing commands.
ACCESS_TOKEN_FILE = os.environ.get("ACCESS_TOKEN_FILE", "/data/access_token")
REFRESH_AHEAD = float(os.environ.get("REFRESH_AHEAD_SECONDS", "1800"))  # refresh this long before expiry

_token_lock = threading.Lock()
_access_token = None
_token_expiry = 0.0

# on/off HA controls -> Fleet's paired start/stop endpoints. {"on": bool} selects which.
# The "off" endpoint may be None (e.g. there is no "sleep" to pair with wake).
TOGGLE_ALIASES = {
    "charge": ("charge_start", "charge_stop"),
    "auto_conditioning": ("auto_conditioning_start", "auto_conditioning_stop"),
    "wake": ("wake_up", None),
}


def resolve_command(command, body):
    """Map a toggle alias + {"on": bool} to a concrete endpoint; returns (endpoint, body).
    endpoint is None when the requested direction has no command (e.g. turning the wake
    switch off)."""
    pair = TOGGLE_ALIASES.get(command)
    if not pair:
        return command, body
    on = bool(body.get("on", True)) if isinstance(body, dict) else True
    return (pair[0] if on else pair[1]), {}


def command_url(command):
    # wake_up is a vehicle endpoint, not a /command/ action.
    if command == "wake_up":
        return "%s/api/1/vehicles/%s/wake_up" % (PROXY_URL, VIN)
    return "%s/api/1/vehicles/%s/command/%s" % (PROXY_URL, VIN, command)


def log(*a):
    print(*a, flush=True)


def _load_persisted_token():
    """Prefer the last rotated token on disk over the (possibly stale) env seed."""
    global REFRESH_TOKEN
    try:
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t and t != REFRESH_TOKEN:
            REFRESH_TOKEN = t
            log("auth: using rotated refresh token from %s" % TOKEN_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        log("auth: could not read %s: %s" % (TOKEN_FILE, e))


def _write_private(path, content):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError as e:
        log("auth: could not write %s: %s" % (path, e))


def _do_refresh(refresh_token):
    data = {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token}
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    return requests.post(AUTH_URL, data=data, timeout=30)


def _refresh_token():
    global _access_token, _token_expiry, REFRESH_TOKEN
    # Try the current (persisted) refresh token first, then fall back to the env seed if it
    # differs. So re-auth is just "update TESLA_REFRESH_TOKEN + redeploy": a dead persisted
    # token self-heals from the env and the volume is re-seeded — no manual file surgery.
    candidates = [REFRESH_TOKEN]
    if ENV_REFRESH_TOKEN and ENV_REFRESH_TOKEN != REFRESH_TOKEN:
        candidates.append(ENV_REFRESH_TOKEN)
    last = None
    for i, tok in enumerate(candidates):
        r = _do_refresh(tok)
        if r.ok:
            j = r.json()
            _access_token = j["access_token"]
            _token_expiry = time.time() + int(j.get("expires_in", 28800)) - 60
            new_rt = j.get("refresh_token") or tok   # Tesla rotates; keep the one we used otherwise
            if new_rt != REFRESH_TOKEN:
                REFRESH_TOKEN = new_rt
                _write_private(TOKEN_FILE, new_rt)
            _write_private(ACCESS_TOKEN_FILE, _access_token)   # share with the helper scripts
            if i > 0:
                log("auth: persisted refresh token failed; recovered from the env token and re-seeded %s" % TOKEN_FILE)
            log("auth: refreshed access token, valid ~%ds" % int(j.get("expires_in", 28800)))
            return _access_token
        last = r
        log("auth: refresh candidate %d/%d rejected: %s %s" % (i + 1, len(candidates), r.status_code, r.text[:160]))
    last.raise_for_status()   # all candidates failed -> surface the last error (re-auth needed)


def get_token(force=False):
    with _token_lock:
        if force or _access_token is None or time.time() >= _token_expiry:
            return _refresh_token()
        return _access_token


def _refresh_loop():
    # Keep the access token fresh ahead of expiry so the helper scripts always read a valid
    # one from ACCESS_TOKEN_FILE, and so the bridge isn't doing a cold refresh mid-command.
    while True:
        with _token_lock:
            wait = _token_expiry - time.time() - REFRESH_AHEAD
        if wait > 0:
            time.sleep(min(wait, 3600))
            continue
        try:
            get_token(force=True)
        except Exception as e:
            log("auth: background refresh error: %s" % e)
            time.sleep(60)


def send_command(command, body):
    url = command_url(command)
    r = None
    for attempt in (1, 2):
        token = get_token(force=(attempt == 2))
        try:
            r = requests.post(
                url,
                json=body,
                headers={"Authorization": "Bearer %s" % token},
                verify=PROXY_CACERT,
                timeout=30,
            )
        except requests.RequestException as e:
            return 0, "request error: %s" % e
        if r.status_code == 401 and attempt == 1:
            log("cmd %s: 401, refreshing token and retrying" % command)
            continue
        return r.status_code, r.text
    return (r.status_code, r.text) if r is not None else (0, "no response")


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        log("mqtt: connect failed: %s" % reason_code)
        return
    client.subscribe(CMD_TOPIC, qos=1)
    log("mqtt: connected, subscribed to %s" % CMD_TOPIC)


def on_message(client, userdata, msg):
    command = msg.topic.rsplit("/", 1)[-1]
    raw = msg.payload.decode("utf-8", "replace").strip()
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        log("cmd %s: bad JSON payload %r, ignoring" % (command, raw))
        return
    endpoint, body = resolve_command(command, body)
    if endpoint is None:
        log("cmd %s: no endpoint for this direction, ignoring" % command)
        return
    status, text = send_command(endpoint, body)
    log("cmd %s -> %s -> %s %s" % (command, endpoint, status, text[:200]))
    client.publish(
        "%s/cmd_result/%s" % (BASE, command),
        json.dumps({"status": status, "resp": text}),
        qos=1,
    )


def main():
    _load_persisted_token()
    try:
        get_token(force=True)
    except Exception as e:
        log("auth: initial token refresh failed (persisted + env both rejected -> re-auth needed): %s" % e)
        sys.exit(1)
    threading.Thread(target=_refresh_loop, daemon=True).start()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tesla-cmd-bridge")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            log("mqtt: loop error: %s, retrying in 10s" % e)
            time.sleep(10)


if __name__ == "__main__":
    main()
