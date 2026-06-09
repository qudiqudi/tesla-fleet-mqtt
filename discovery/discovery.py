#!/usr/bin/env python3
"""
tesla-ha-discovery: publish Home Assistant MQTT discovery configs (retained) so the car
shows up in HA as a device with sensors, binary sensors, switches, numbers and a device
tracker -- fed by the fleet-telemetry stream instead of polling.

Entities point at:
  - raw fields   <BASE>/<VIN>/v/<Field>     (published by fleet-telemetry, JSON-encoded)
  - derived state <BASE>/<VIN>/ha/<name>    (published by tlwriter with HA_PUBLISH=1:
                                             normalised distances, session state, gps, ...)
  - commands     <BASE>/cmd/<command>       (handled by tesla-cmd-bridge)

The service is stateless: it republishes every HA_DISCOVERY_INTERVAL seconds (and on
connect) so the configs survive a broker restart and reclaim the entities if another
publisher (e.g. a teslalogger you are migrating off) overwrites them.

Discovery topics and unique_ids match the teslalogger MQTT layout
(homeassistant/<comp>/<VIN>/<object_id>/config, unique_id <VIN>_<object_id>) so an existing
teslalogger-based dashboard keeps working unchanged after you point it here.
"""
import json
import os
import time

import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "tesla")
MQTT_PASS = os.environ["MQTT_PASSWORD"]
BASE = os.environ.get("MQTT_TOPIC_BASE", "tesla").rstrip("/")
VIN = os.environ["TESLA_VIN"]

PREFIX = os.environ.get("HA_DISCOVERY_PREFIX", "homeassistant").rstrip("/")
DEV_NAME = os.environ.get("HA_DEVICE_NAME", "Tesla")
DEV_MODEL = os.environ.get("HA_DEVICE_MODEL", "Tesla")
DEV_MFR = os.environ.get("HA_DEVICE_MANUFACTURER", "Tesla")
INTERVAL = float(os.environ.get("HA_DISCOVERY_INTERVAL", "300"))

DEVICE = {"identifiers": [VIN], "manufacturer": DEV_MFR, "model": DEV_MODEL, "name": DEV_NAME}


def log(*a):
    print(*a, flush=True)


def v(field):
    return "%s/%s/v/%s" % (BASE, VIN, field)


def ha(name):
    return "%s/%s/ha/%s" % (BASE, VIN, name)


def cmd(command):
    return "%s/cmd/%s" % (BASE, command)


# Each entry: (component, object_id, config-without-device-or-unique_id).
# unique_id and device are filled in by build(). Topics use full HA keys for clarity.
ENTITIES = [
    # --- sensors -----------------------------------------------------------
    ("sensor", "state", {"name": "State", "state_topic": ha("state"), "icon": "mdi:map"}),
    ("sensor", "battery_level", {"name": "Battery Level", "state_topic": v("BatteryLevel"),
                                 "device_class": "battery", "unit_of_measurement": "%",
                                 "state_class": "measurement",
                                 "value_template": "{{ value | float | round(0) | int }}"}),
    ("sensor", "odometer", {"name": "Odometer", "state_topic": ha("odometer_km"),
                            "device_class": "distance", "unit_of_measurement": "km",
                            "state_class": "total_increasing"}),
    ("sensor", "speed", {"name": "Speed", "state_topic": ha("speed_kmh"),
                         "device_class": "speed", "unit_of_measurement": "km/h"}),
    ("sensor", "battery_range_km", {"name": "Battery range", "state_topic": ha("battery_range_km"),
                                    "device_class": "distance", "unit_of_measurement": "km"}),
    ("sensor", "ideal_battery_range_km", {"name": "Ideal battery range",
                                          "state_topic": ha("ideal_range_km"),
                                          "device_class": "distance", "unit_of_measurement": "km"}),
    ("sensor", "charge_rate_km", {"name": "Charge rate", "state_topic": ha("charge_rate_km"),
                                  "device_class": "speed", "unit_of_measurement": "km/h"}),
    ("sensor", "charger_power", {"name": "Charge power", "state_topic": ha("charger_power_kw"),
                                 "device_class": "power", "unit_of_measurement": "kW"}),
    ("sensor", "charge_energy_added", {"name": "Energy added", "state_topic": ha("energy_added_kwh"),
                                       "device_class": "energy", "unit_of_measurement": "kWh",
                                       "state_class": "total_increasing"}),
    ("sensor", "inside_temperature", {"name": "Inside temperature", "state_topic": v("InsideTemp"),
                                      "device_class": "temperature", "unit_of_measurement": "°C",
                                      "value_template": "{{ value | float | round(1) }}"}),
    ("sensor", "outside_temp", {"name": "Outside temperature", "state_topic": v("OutsideTemp"),
                                "device_class": "temperature", "unit_of_measurement": "°C",
                                "value_template": "{{ value | float | round(1) }}"}),
    ("sensor", "open_doors", {"name": "Doors opened", "state_topic": ha("open_doors"),
                              "icon": "mdi:car-door"}),
    ("sensor", "frunk", {"name": "Frunk opened", "state_topic": ha("frunk"), "icon": "mdi:car-door",
                         "value_template": "{{ 'Open' if value == 'true' else 'Closed' }}"}),
    ("sensor", "trunk", {"name": "Trunk opened", "state_topic": ha("trunk"), "icon": "mdi:car-door",
                         "value_template": "{{ 'Open' if value == 'true' else 'Closed' }}"}),
    ("sensor", "latitude", {"name": "Latitude", "state_topic": v("Location"), "unit_of_measurement": "°",
                            "icon": "mdi:latitude", "value_template": "{{ value_json.latitude }}"}),
    ("sensor", "longitude", {"name": "Longitude", "state_topic": v("Location"), "unit_of_measurement": "°",
                             "icon": "mdi:longitude", "value_template": "{{ value_json.longitude }}"}),
    # --- binary sensors ----------------------------------------------------
    # Locked: raw Locked is true when locked; lock device_class is ON when unlocked.
    ("binary_sensor", "locked", {"name": "Locked", "state_topic": v("Locked"), "device_class": "lock",
                                 "payload_on": "false", "payload_off": "true"}),
    ("binary_sensor", "sleeping", {"name": "Sleeping", "state_topic": ha("sleeping"),
                                   "payload_on": "true", "payload_off": "false", "icon": "mdi:sleep"}),
    ("binary_sensor", "driving", {"name": "Driving", "state_topic": ha("driving"),
                                  "device_class": "moving", "payload_on": "true", "payload_off": "false"}),
    ("binary_sensor", "plugged_in", {"name": "Plugged in", "state_topic": ha("plugged_in"),
                                     "device_class": "plug", "payload_on": "true", "payload_off": "false"}),
    ("binary_sensor", "fast_charger_present", {"name": "Fast charger", "state_topic": ha("fast_charger"),
                                               "payload_on": "true", "payload_off": "false",
                                               "icon": "mdi:ev-station"}),
    ("binary_sensor", "charge_port_door_open", {"name": "Charge port opened",
                                                "state_topic": v("ChargePortDoorOpen"),
                                                "device_class": "opening", "payload_on": "true",
                                                "payload_off": "false", "icon": "mdi:ev-plug-ccs2"}),
    ("binary_sensor", "battery_heater", {"name": "Battery heater", "state_topic": v("BatteryHeaterOn"),
                                         "device_class": "heat", "payload_on": "true",
                                         "payload_off": "false"}),
    # --- switches ----------------------------------------------------------
    ("switch", "charging", {"name": "Charging", "state_topic": ha("charging"),
                            "state_on": "true", "state_off": "false", "command_topic": cmd("charge"),
                            "payload_on": '{"on": true}', "payload_off": '{"on": false}'}),
    # fleet-telemetry publishes string fields JSON-encoded (the payload is "HvacPowerStateOff"
    # *with quotes*), so compare value_json, not the raw value.
    ("switch", "is_preconditioning", {"name": "Preconditioning", "state_topic": v("HvacPower"),
                                      "value_template": "{{ 'off' if value_json == 'HvacPowerStateOff' else 'on' }}",
                                      "state_on": "on", "state_off": "off",
                                      "command_topic": cmd("auto_conditioning"),
                                      "payload_on": '{"on": true}', "payload_off": '{"on": false}',
                                      "icon": "mdi:heat-wave"}),
    ("switch", "sentry_mode", {"name": "Sentry mode", "state_topic": v("SentryMode"),
                               "value_template": "{{ 'off' if value_json == 'SentryModeStateOff' else 'on' }}",
                               "state_on": "on", "state_off": "off",
                               "command_topic": cmd("set_sentry_mode"),
                               "payload_on": '{"on": true}', "payload_off": '{"on": false}',
                               "icon": "mdi:cctv"}),
    ("switch", "online", {"name": "Online", "state_topic": ha("online"), "state_on": "true",
                          "state_off": "false", "command_topic": cmd("wake"),
                          "payload_on": '{"on": true}', "payload_off": '{"on": false}',
                          "icon": "mdi:car-connected"}),
    # --- numbers -----------------------------------------------------------
    ("number", "charge_limit_soc", {"name": "Charge limit SoC", "state_topic": v("ChargeLimitSoc"),
                                    "command_topic": cmd("set_charge_limit"),
                                    "command_template": '{"percent": {{ value | int }}}',
                                    "device_class": "battery", "unit_of_measurement": "%",
                                    "min": 50, "max": 100, "step": 1}),
    ("number", "charge_current_request", {"name": "Charge current request",
                                          "state_topic": v("ChargeCurrentRequest"),
                                          "command_topic": cmd("set_charging_amps"),
                                          "command_template": '{"charging_amps": {{ value | int }}}',
                                          "device_class": "current", "unit_of_measurement": "A",
                                          "min": 0, "max": 32, "step": 1}),
    # --- tpms (built below) ------------------------------------------------
]

for _side, _name in (("fl", "Front left"), ("fr", "Front right"),
                     ("rl", "Rear left"), ("rr", "Rear right")):
    ENTITIES.append(("sensor", "tpms_pressure_%s" % _side, {
        "name": "TPMS %s" % _name, "state_topic": v("TpmsPressure%s" % _side.upper()),
        "device_class": "pressure", "unit_of_measurement": "bar", "icon": "mdi:tire",
        "value_template": "{{ value | float | round(2) }}"}))


def build_configs():
    out = []
    for comp, obj, cfg in ENTITIES:
        c = dict(cfg)
        c["unique_id"] = "%s_%s" % (VIN, obj)
        c["device"] = DEVICE
        topic = "%s/%s/%s/%s/config" % (PREFIX, comp, VIN, obj)
        out.append((topic, c))
    # device_tracker: GPS via json attributes, home/not_home from <BASE>/<VIN>/ha/home.
    # No unique_id (matches teslalogger; keeps the same device_tracker.<name> entity).
    out.append(("%s/device_tracker/%s/config" % (PREFIX, VIN), {
        "name": DEV_NAME, "state_topic": ha("home"), "json_attributes_topic": ha("gps"),
        "source_type": "gps", "payload_home": "home", "payload_not_home": "not_home",
        "device": DEVICE}))
    return out


def publish_all(client):
    configs = build_configs()
    for topic, cfg in configs:
        client.publish(topic, json.dumps(cfg, ensure_ascii=False), qos=1, retain=True)
    log("published %d discovery configs for %s" % (len(configs), VIN))


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        log("mqtt: connect failed: %s" % reason_code)
        return
    log("mqtt: connected to %s:%s" % (MQTT_HOST, MQTT_PORT))
    publish_all(client)


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tesla-ha-discovery")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    try:
        while True:
            time.sleep(INTERVAL)
            if client.is_connected():
                publish_all(client)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()


if __name__ == "__main__":
    main()
