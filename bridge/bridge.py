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
REFRESH_TOKEN = os.environ["TESLA_REFRESH_TOKEN"]
AUTH_URL = os.environ.get("TESLA_AUTH_URL", "https://auth.tesla.com/oauth2/v3/token")
TOKEN_FILE = os.environ.get("REFRESH_TOKEN_FILE", "/data/refresh_token")

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


def _persist_token(t):
    try:
        tmp = TOKEN_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(t)
        os.chmod(tmp, 0o600)
        os.replace(tmp, TOKEN_FILE)
    except OSError as e:
        log("auth: could not persist rotated token to %s: %s" % (TOKEN_FILE, e))


def _refresh_token():
    global _access_token, _token_expiry, REFRESH_TOKEN
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": REFRESH_TOKEN,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    r = requests.post(AUTH_URL, data=data, timeout=30)
    if not r.ok:
        log("auth: token endpoint %s: %s" % (r.status_code, r.text[:200]))
    r.raise_for_status()
    j = r.json()
    _access_token = j["access_token"]
    _token_expiry = time.time() + int(j.get("expires_in", 28800)) - 60
    new_rt = j.get("refresh_token")
    if new_rt and new_rt != REFRESH_TOKEN:
        REFRESH_TOKEN = new_rt
        _persist_token(new_rt)
        log("auth: stored rotated refresh token")
    log("auth: refreshed access token, valid ~%ds" % int(j.get("expires_in", 28800)))
    return _access_token


def get_token(force=False):
    with _token_lock:
        if force or _access_token is None or time.time() >= _token_expiry:
            return _refresh_token()
        return _access_token


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
        log("auth: initial token refresh failed: %s" % e)
        sys.exit(1)

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
