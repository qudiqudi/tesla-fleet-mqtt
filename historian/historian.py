#!/usr/bin/env python3
"""
tesla-influx: subscribe to <base>/<vin>/v/# and write each telemetry value to InfluxDB 2.x.

Each MQTT message is one field update; it's written as a point:
  measurement=<INFLUX_MEASUREMENT> (default "vehicle"), tag vin=<VIN>, field=<Field>=<value>.
Handles the mixed Fleet Telemetry payloads:
  - numbers            -> float field
  - true/false         -> bool field
  - Location JSON       -> Latitude / Longitude float fields
  - object JSON (e.g. DoorState) -> flattened <Field>_<key> fields
  - strings (ChargeState, SentryMode, ...) -> string field (kept for state panels)
"""
import json
import os
import time

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "tesla")
MQTT_PASS = os.environ["MQTT_PASSWORD"]
BASE = os.environ.get("MQTT_TOPIC_BASE", "tesla").rstrip("/")

INFLUX_URL = os.environ.get("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.environ["INFLUX_TOKEN"]
INFLUX_ORG = os.environ["INFLUX_ORG"]
INFLUX_BUCKET = os.environ.get("INFLUX_BUCKET", "tesla")
MEASUREMENT = os.environ.get("INFLUX_MEASUREMENT", "vehicle")

influx = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
write_api = influx.write_api(write_options=SYNCHRONOUS)


def log(*a):
    print(*a, flush=True)


def build_point(vin, field, raw):
    p = Point(MEASUREMENT).tag("vin", vin)
    raw = raw.strip()
    if raw == "" or raw == "null":
        return None
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        v = raw
    if isinstance(v, bool):
        return p.field(field, v)
    if isinstance(v, (int, float)):
        return p.field(field, float(v))
    if isinstance(v, dict):
        if "latitude" in v and "longitude" in v:
            return p.field("Latitude", float(v["latitude"])).field("Longitude", float(v["longitude"]))
        wrote = False
        for k, val in v.items():
            if isinstance(val, bool):
                p.field("%s_%s" % (field, k), val); wrote = True
            elif isinstance(val, (int, float)):
                p.field("%s_%s" % (field, k), float(val)); wrote = True
            elif val is not None:
                p.field("%s_%s" % (field, k), str(val)); wrote = True
        return p if wrote else None
    # plain string: keep numeric strings numeric, else store as string
    try:
        return p.field(field, float(v))
    except (TypeError, ValueError):
        return p.field(field, str(v))


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        log("mqtt: connect failed: %s" % reason_code); return
    topic = "%s/+/v/#" % BASE
    client.subscribe(topic, qos=1)
    log("mqtt: connected, subscribed to %s -> influx %s/%s" % (topic, INFLUX_ORG, INFLUX_BUCKET))


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    try:
        vi = parts.index("v")
        vin = parts[vi - 1]
        field = parts[vi + 1]
    except (ValueError, IndexError):
        return
    pt = build_point(vin, field, msg.payload.decode("utf-8", "replace"))
    if pt is None:
        return
    try:
        write_api.write(bucket=INFLUX_BUCKET, record=pt)
    except Exception as e:
        log("influx write error for %s: %s" % (field, e))


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tesla-influx")
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
