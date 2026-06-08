#!/usr/bin/env python3
"""
tesla-sessionizer: turn the raw MQTT telemetry stream into discrete drive / charge /
park sessions and store them in MariaDB (tables drives, charges, parks).

State machine per VIN (charge and drive are mutually exclusive in practice):
  - charging  while ACChargingPower or DCChargingPower > CHARGE_POWER_MIN
  - driving   while VehicleSpeed > DRIVE_SPEED_MIN  (brief stops stay in the drive)
  - parked    otherwise
A drive ends after DRIVE_END_TIMEOUT s without movement; a charge after
CHARGE_END_TIMEOUT s without power. Sessions are written idempotently (unique vin+start_ts).

Thresholds are env-tunable. This is a pragmatic v1; tune for your car/usage.
"""
import json
import os
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import pymysql

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "tesla")
MQTT_PASS = os.environ["MQTT_PASSWORD"]
BASE = os.environ.get("MQTT_TOPIC_BASE", "tesla").rstrip("/")

DB_HOST = os.environ.get("DB_HOST", "mariadb")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "tesla")
DB_PASS = os.environ["DB_PASSWORD"]
DB_NAME = os.environ.get("DB_NAME", "tesla")

CHARGE_POWER_MIN = float(os.environ.get("CHARGE_POWER_MIN", "0.5"))     # kW
DRIVE_SPEED_MIN = float(os.environ.get("DRIVE_SPEED_MIN", "1.0"))       # km/h
DRIVE_END_TIMEOUT = float(os.environ.get("DRIVE_END_TIMEOUT", "300"))   # s
CHARGE_END_TIMEOUT = float(os.environ.get("CHARGE_END_TIMEOUT", "180")) # s
MIN_DRIVE_KM = float(os.environ.get("MIN_DRIVE_KM", "0.1"))
TICK_S = float(os.environ.get("TICK_S", "20"))

NUMERIC = {"VehicleSpeed", "ACChargingPower", "DCChargingPower", "Soc", "Odometer",
           "Latitude", "Longitude", "OutsideTemp", "InsideTemp",
           "ACChargingEnergyIn", "DCChargingEnergyIn", "RatedRange"}


def is_drive_gear(g):
    # teslalogger drives on shift state R/N/D; park on P. Fleet telemetry Gear may be
    # "D"/"R"/"N"/"P" or an enum string like "ShiftStateD".
    if g is None:
        return False
    s = str(g).upper()
    return s.endswith("D") or s.endswith("R") or s.endswith("N")

lock = threading.Lock()
latest = {}    # vin -> {field: value}
active = {}    # vin -> {"drive": ts, "charge": ts}
state = {}     # vin -> {"mode": str, "session": dict|None}


def log(*a):
    print(*a, flush=True)


def now():
    return time.time()


def dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_db = None


def db():
    global _db
    if _db is None:
        _db = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
                              database=DB_NAME, autocommit=True, connect_timeout=10)
    return _db


def write(sql, params):
    global _db
    for attempt in (1, 2):
        try:
            with db().cursor() as cur:
                cur.execute(sql, params)
            return
        except Exception as e:
            log("db error (attempt %d): %s" % (attempt, e))
            _db = None
            time.sleep(1)


def lv(vin, field):
    return latest.get(vin, {}).get(field)


# ---- session open/close ---------------------------------------------------

def open_session(vin, mode, ts):
    s = {"start": ts, "start_soc": lv(vin, "Soc"), "start_odo": lv(vin, "Odometer"),
         "start_lat": lv(vin, "Latitude"), "start_lng": lv(vin, "Longitude"),
         "max_speed": lv(vin, "VehicleSpeed") or 0.0, "max_power": 0.0,
         "charger_type": None, "outside_temp": lv(vin, "OutsideTemp"),
         "start_energy": (lv(vin, "ACChargingEnergyIn") or 0.0) + (lv(vin, "DCChargingEnergyIn") or 0.0)}
    state[vin] = {"mode": mode, "session": s}
    log("%s %s start @ %s soc=%s" % (vin, mode, dt(ts), s["start_soc"]))


def close_session(vin, end_ts):
    st = state.get(vin)
    if not st or not st["session"]:
        return
    mode, s = st["mode"], st["session"]
    dur = max(0, int(end_ts - s["start"]))
    end_soc = lv(vin, "Soc")
    if mode == "drive":
        end_odo = lv(vin, "Odometer")
        dist = (end_odo - s["start_odo"]) if (end_odo is not None and s["start_odo"] is not None) else None
        if dist is not None and dist < MIN_DRIVE_KM:
            log("%s drive discarded (%.2f km)" % (vin, dist)); return
        avg = (dist / (dur / 3600.0)) if (dist and dur > 0) else None
        write("""INSERT INTO drives (vin,start_ts,end_ts,duration_s,start_odometer,end_odometer,
                 distance_km,start_soc,end_soc,soc_used,start_lat,start_lng,end_lat,end_lng,
                 max_speed,avg_speed,outside_temp)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                 ON DUPLICATE KEY UPDATE end_ts=VALUES(end_ts),duration_s=VALUES(duration_s),
                 end_odometer=VALUES(end_odometer),distance_km=VALUES(distance_km),
                 end_soc=VALUES(end_soc),soc_used=VALUES(soc_used),end_lat=VALUES(end_lat),
                 end_lng=VALUES(end_lng),max_speed=VALUES(max_speed),avg_speed=VALUES(avg_speed)""",
              (vin, dt(s["start"]), dt(end_ts), dur, s["start_odo"], end_odo, dist,
               s["start_soc"], end_soc,
               (s["start_soc"] - end_soc) if (s["start_soc"] is not None and end_soc is not None) else None,
               s["start_lat"], s["start_lng"], lv(vin, "Latitude"), lv(vin, "Longitude"),
               s["max_speed"], avg, s["outside_temp"]))
        log("%s drive end dist=%s km dur=%ss" % (vin, round(dist, 2) if dist else None, dur))
    elif mode == "charge":
        added = (end_soc - s["start_soc"]) if (end_soc is not None and s["start_soc"] is not None) else None
        # Energy added = cumulative charge-energy delta (teslalogger's accurate method).
        end_energy = (lv(vin, "ACChargingEnergyIn") or 0.0) + (lv(vin, "DCChargingEnergyIn") or 0.0)
        kwh = (end_energy - s["start_energy"]) if end_energy >= s["start_energy"] else None
        write("""INSERT INTO charges (vin,start_ts,end_ts,duration_s,start_soc,end_soc,soc_added,
                 energy_added_kwh,max_power_kw,charger_type,lat,lng)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                 ON DUPLICATE KEY UPDATE end_ts=VALUES(end_ts),duration_s=VALUES(duration_s),
                 end_soc=VALUES(end_soc),soc_added=VALUES(soc_added),energy_added_kwh=VALUES(energy_added_kwh),
                 max_power_kw=VALUES(max_power_kw),charger_type=VALUES(charger_type)""",
              (vin, dt(s["start"]), dt(end_ts), dur, s["start_soc"], end_soc, added, kwh,
               s["max_power"], s["charger_type"], s["start_lat"], s["start_lng"]))
        log("%s charge end +%s%% %skWh maxP=%skW dur=%ss" % (vin, round(added, 1) if added else None,
                                                            round(kwh, 1) if kwh else None,
                                                            round(s["max_power"], 1), dur))
    elif mode == "park":
        loss = (s["start_soc"] - end_soc) if (s["start_soc"] is not None and end_soc is not None) else None
        write("""INSERT INTO parks (vin,start_ts,end_ts,duration_s,start_soc,end_soc,soc_loss,lat,lng)
                 VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                 ON DUPLICATE KEY UPDATE end_ts=VALUES(end_ts),duration_s=VALUES(duration_s),
                 end_soc=VALUES(end_soc),soc_loss=VALUES(soc_loss)""",
              (vin, dt(s["start"]), dt(end_ts), dur, s["start_soc"], end_soc, loss,
               s["start_lat"], s["start_lng"]))
    state[vin] = {"mode": None, "session": None}


def transition(vin, new_mode, ts):
    cur = state.get(vin, {}).get("mode")
    if cur == new_mode:
        return
    close_session(vin, ts)
    open_session(vin, new_mode, ts)


# ---- mqtt -----------------------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        log("mqtt: connect failed: %s" % reason_code); return
    client.subscribe("%s/+/v/#" % BASE, qos=1)
    log("mqtt: connected, building sessions -> %s@%s/%s" % (DB_USER, DB_HOST, DB_NAME))


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    try:
        vi = parts.index("v"); vin = parts[vi - 1]; field = parts[vi + 1]
    except (ValueError, IndexError):
        return
    raw = msg.payload.decode("utf-8", "replace").strip()
    if raw in ("", "null"):
        return
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        val = raw
    if field == "Location" and isinstance(val, dict):
        with lock:
            latest.setdefault(vin, {})["Latitude"] = val.get("latitude")
            latest.setdefault(vin, {})["Longitude"] = val.get("longitude")
        return
    with lock:
        latest.setdefault(vin, {})[field] = val
        active.setdefault(vin, {})
        t = now()
        if field == "VehicleSpeed" and isinstance(val, (int, float)) and val > DRIVE_SPEED_MIN:
            active[vin]["drive"] = t
            if state.get(vin, {}).get("mode") != "charge":
                transition(vin, "drive", t)
            st = state.get(vin, {}).get("session")
            if st and val > st["max_speed"]:
                st["max_speed"] = float(val)
        elif field in ("ACChargingPower", "DCChargingPower") and isinstance(val, (int, float)) and val > CHARGE_POWER_MIN:
            active[vin]["charge"] = t
            transition(vin, "charge", t)
            st = state.get(vin, {}).get("session")
            if st:
                if val > st["max_power"]:
                    st["max_power"] = float(val)
                st["charger_type"] = "AC" if field == "ACChargingPower" else "DC"
        elif field == "Gear" and is_drive_gear(val):
            # shift state R/N/D -> driving (teslalogger's drive trigger), unless charging
            active[vin]["drive"] = t
            if state.get(vin, {}).get("mode") != "charge":
                transition(vin, "drive", t)


def ticker():
    while True:
        time.sleep(TICK_S)
        t = now()
        with lock:
            for vin, st in list(state.items()):
                mode = st.get("mode")
                a = active.get(vin, {})
                if mode == "drive" and (t - a.get("drive", 0)) > DRIVE_END_TIMEOUT:
                    transition(vin, "park", a.get("drive", t))
                elif mode == "charge" and (t - a.get("charge", 0)) > CHARGE_END_TIMEOUT:
                    transition(vin, "park", a.get("charge", t))


def main():
    threading.Thread(target=ticker, daemon=True).start()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tesla-sessionizer")
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
