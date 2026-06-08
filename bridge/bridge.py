#!/usr/bin/env python3
"""
tesla-cmd-bridge: subscribe to <base>/cmd/# on MQTT and forward each message
as a signed Fleet API command via tesla-http-proxy.

Topic:   <base>/cmd/<command>      e.g. tesla/cmd/set_sentry_mode
Payload: JSON body for the command, or empty for none.
         {"on": true}              -> tesla/cmd/set_sentry_mode
         {}                        -> tesla/cmd/auto_conditioning_start
Result:  published to <base>/cmd_result/<command> as {"status": <int>, "resp": <str>}

The long-lived refresh token is read from the environment; the bridge exchanges it
for short-lived access tokens and refreshes on expiry / 401.
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

_token_lock = threading.Lock()
_access_token = None
_token_expiry = 0.0


def log(*a):
    print(*a, flush=True)


def _refresh_token():
    global _access_token, _token_expiry
    data = {
        "grant_type": "refresh_token",
        "client_id": CLIENT_ID,
        "refresh_token": REFRESH_TOKEN,
    }
    if CLIENT_SECRET:
        data["client_secret"] = CLIENT_SECRET
    r = requests.post(AUTH_URL, data=data, timeout=30)
    r.raise_for_status()
    j = r.json()
    _access_token = j["access_token"]
    _token_expiry = time.time() + int(j.get("expires_in", 28800)) - 60
    log("auth: refreshed access token, valid ~%ds" % int(j.get("expires_in", 28800)))
    return _access_token


def get_token(force=False):
    with _token_lock:
        if force or _access_token is None or time.time() >= _token_expiry:
            return _refresh_token()
        return _access_token


def send_command(command, body):
    url = "%s/api/1/vehicles/%s/command/%s" % (PROXY_URL, VIN, command)
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
    status, text = send_command(command, body)
    log("cmd %s -> %s %s" % (command, status, text[:200]))
    client.publish(
        "%s/cmd_result/%s" % (BASE, command),
        json.dumps({"status": status, "resp": text}),
        qos=1,
    )


def main():
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
