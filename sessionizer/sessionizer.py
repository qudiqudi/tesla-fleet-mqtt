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
REPLAY_GRACE = float(os.environ.get("REPLAY_GRACE", "10"))  # ignore retained replay for Ns after connect

# Fields consumed as numbers below. Some firmwares marshal numerics as JSON strings ("4.5");
# coerce in on_message, and never store a non-numeric value that would crash the session math.
NUMERIC = {"VehicleSpeed", "ACChargingPower", "DCChargingPower", "Soc", "Odometer",
           "Latitude", "Longitude", "OutsideTemp", "InsideTemp",
           "ACChargingEnergyIn", "DCChargingEnergyIn", "RatedRange"}
# Power/speed stream null when inactive; that null IS the live value (0), not a gap.
NULL_ZERO_FIELDS = {"VehicleSpeed", "ACChargingPower", "DCChargingPower"}

MI_TO_KM = 1.609344
# Same odometer-continuity unit detection as tlwriter: distance/speed stream in the car's
# display unit and a firmware update can flip it to miles (regression seen 2026-06). The
# odometer never drops, so a reading well below the known km figure means the stream went
# imperial (~0.62x); convert until it recovers. Reference seeded from the drives table.
DIST_FIELDS = {"VehicleSpeed", "Odometer", "RatedRange"}
IMP_CUTOFF = float(os.environ.get("IMPERIAL_CUTOFF", "0.75"))
unit_ref = {}   # vin -> highest odometer seen, in km
unit_imp = {}   # vin -> True if telemetry currently looks imperial


def detect_unit(vin, raw_odo):
    ref = unit_ref.get(vin, 0.0)
    if ref > 0 and raw_odo > 0:
        if raw_odo < IMP_CUTOFF * ref and not unit_imp.get(vin):
            unit_imp[vin] = True
            log("%s telemetry imperial (odo %.0f vs %.0f km) -> converting" % (vin, raw_odo, ref))
        elif raw_odo >= 0.9 * ref and unit_imp.get(vin):
            unit_imp[vin] = False
            log("%s telemetry metric again (odo %.0f) -> conversion off" % (vin, raw_odo))
    km = raw_odo * MI_TO_KM if unit_imp.get(vin) else raw_odo
    if km > ref:
        unit_ref[vin] = km


def gear_letter(g):
    # Gear streams "D"/"R"/"N"/"P" or an enum string like "ShiftStateD". Strip the prefix and
    # match exactly — suffix matching classified ShiftStateInvalid (ends in D) as driving.
    if g is None:
        return None
    s = str(g)
    if s.startswith("ShiftState"):
        s = s[len("ShiftState"):]
    s = s.upper()
    return s if s in ("P", "D", "R", "N") else None


def is_drive_gear(g):
    # teslalogger drives on shift state R/N/D; park on P.
    return gear_letter(g) in ("D", "R", "N")

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


_pending = []   # failed session writes, retried from the ticker — a finished session row
                # must survive the DB being down at the moment the session closes


def write(sql, params):
    # mariadb drops the idle connection while parked; reconnect and retry once, quietly.
    global _db
    for attempt in (1, 2):
        try:
            with db().cursor() as cur:
                cur.execute(sql, params)
            return
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            _db = None  # stale/dropped connection -> fresh connection on the retry
        except Exception as e:
            log("db error: %s" % e); _db = None; break
    # session writes are idempotent upserts (unique vin+start_ts), so queue and retry from
    # the ticker instead of losing the session forever
    if len(_pending) < 1000:
        _pending.append((sql, params))
    log("db write failed, queued for retry (%d pending)" % len(_pending))


def flush_pending():
    global _db
    while _pending:
        sql, params = _pending[0]
        try:
            with db().cursor() as cur:
                cur.execute(sql, params)
        except Exception:
            _db = None  # still down; keep the queue and try again next tick
            return
        _pending.pop(0)


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

_connect_ts = 0.0


def on_connect(client, userdata, flags, reason_code, properties=None):
    global _connect_ts
    if reason_code != 0:
        log("mqtt: connect failed: %s" % reason_code); return
    _connect_ts = now()
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
        if field not in NULL_ZERO_FIELDS:
            return
        val = 0
    else:
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
    if field in NUMERIC and not isinstance(val, (int, float)):
        try:
            val = float(val)
        except (TypeError, ValueError):
            return   # garbage in a numeric field: never store it
    t = now()
    # The publisher retains every message, so the broker REPLAYS the last value of each topic
    # on (re)subscribe. That burst right after connect is stale state, not live activity — it
    # must not open a ghost drive/charge from a past session's retained speed/power.
    replay = bool(getattr(msg, "retain", False)) and (t - _connect_ts) < REPLAY_GRACE
    if field == "Location" and isinstance(val, dict):
        with lock:
            latest.setdefault(vin, {})["Latitude"] = val.get("latitude")
            latest.setdefault(vin, {})["Longitude"] = val.get("longitude")
        return
    with lock:
        if field == "Odometer":
            detect_unit(vin, val)
        if field in DIST_FIELDS and unit_imp.get(vin):
            val = val * MI_TO_KM   # normalise miles/(mph) to km/(km-h)
        latest.setdefault(vin, {})[field] = val
        active.setdefault(vin, {})
        if replay:   # value kept for last-known lookups, but no session activity from a replay
            return
        if ((field == "VehicleSpeed" and val > DRIVE_SPEED_MIN)
                or (field == "Gear" and is_drive_gear(val))):
            # movement or shift state R/N/D -> driving (teslalogger's triggers), unless charging
            active[vin]["drive"] = t
            if state.get(vin, {}).get("mode") != "charge":
                transition(vin, "drive", t)
            st = state.get(vin, {}).get("session")
            if st and field == "VehicleSpeed" and val > st["max_speed"]:
                st["max_speed"] = float(val)
        elif field in ("ACChargingPower", "DCChargingPower") and val > CHARGE_POWER_MIN:
            active[vin]["charge"] = t
            transition(vin, "charge", t)
            st = state.get(vin, {}).get("session")
            if st:
                if val > st["max_power"]:
                    st["max_power"] = float(val)
                st["charger_type"] = "AC" if field == "ACChargingPower" else "DC"


def ticker():
    while True:
        time.sleep(TICK_S)
        t = now()
        try:
            with lock:
                flush_pending()
                for vin, st in list(state.items()):
                    mode = st.get("mode")
                    a = active.get(vin, {})
                    if mode == "drive" and (t - a.get("drive", 0)) > DRIVE_END_TIMEOUT:
                        transition(vin, "park", a.get("drive", t))
                    elif mode == "charge" and (t - a.get("charge", 0)) > CHARGE_END_TIMEOUT:
                        transition(vin, "park", a.get("charge", t))
        except Exception as e:
            # the ticker is the only thing that closes timed-out sessions; it must survive
            log("ticker error: %r" % e)


def seed_unit_ref():
    # last known km odometer per vin -> reference for the miles-vs-km detection
    try:
        with db().cursor() as cur:
            cur.execute("SELECT vin, MAX(end_odometer) FROM drives GROUP BY vin")
            for vin, odo in cur.fetchall():
                if odo:
                    unit_ref[vin] = float(odo)
                    log("%s odometer reference: %.0f km" % (vin, unit_ref[vin]))
    except Exception as e:
        log("unit reference seed failed (detection starts cold): %s" % e)


def main():
    seed_unit_ref()
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
