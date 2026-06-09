#!/usr/bin/env python3
"""
tesla-tlwriter: write fleet telemetry into teslalogger's schema (pos / drivestate /
chargingstate / charging) so teslalogger's own Grafana dashboards stay live on our data.

Writes to a copy of the teslalogger DB (DB_NAME, default 'teslalogger'). teslalogger can
run in parallel against its own DB so you can diff and confirm the writer matches.

Model (mirrors teslalogger):
  - pos: a row every POS_*_INTERVAL while online (Datum/lat/lng required) with current values.
  - drivestate: opened on shift R/N/D or movement (StartPos = current pos id), closed after
    an idle timeout (EndPos = current pos id) with speed_max / TPMS / outside_temp.
  - charging: a row per CHARGE_ROW_INTERVAL while charging.
  - chargingstate: opened at charge start (StartChargingID), closed at charge end
    (EndChargingID, charge_energy_added = cumulative delta, max_charger_power, fast_charger_type).
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
DB_NAME = os.environ.get("TLW_DB_NAME", "teslalogger")
VIN = os.environ["TESLA_VIN"]

CHARGE_POWER_MIN = float(os.environ.get("CHARGE_POWER_MIN", "0.5"))
DRIVE_SPEED_MIN = float(os.environ.get("DRIVE_SPEED_MIN", "1.0"))
DRIVE_END_TIMEOUT = float(os.environ.get("DRIVE_END_TIMEOUT", "300"))
CHARGE_END_TIMEOUT = float(os.environ.get("CHARGE_END_TIMEOUT", "180"))
ONLINE_TIMEOUT = float(os.environ.get("ONLINE_TIMEOUT", "180"))
TICK_S = float(os.environ.get("TICK_S", "10"))
POS_DRIVE_INTERVAL = float(os.environ.get("POS_DRIVE_INTERVAL", "10"))
POS_CHARGE_INTERVAL = float(os.environ.get("POS_CHARGE_INTERVAL", "60"))
POS_IDLE_INTERVAL = float(os.environ.get("POS_IDLE_INTERVAL", "600"))
CHARGE_ROW_INTERVAL = float(os.environ.get("CHARGE_ROW_INTERVAL", "60"))

MI_TO_KM = 1.609344
# Distance/speed fields stream in the car's display unit; the teslalogger schema stores
# km / km-h, so convert when the car reports miles. Tesla doesn't document the unit and a
# firmware update can flip it (regression seen 2026-06: Odometer streamed in miles while the
# car was set to km), so "auto" doesn't trust SettingDistanceUnit — it detects from the
# odometer itself: that's monotonic, and we know the true km from history, so a reading well
# below the known km figure is miles (~0.62x). Self-corrects both ways, no manual toggle.
# TLW_DISTANCE_UNIT: auto (detect, default) | metric | imperial (force, escape hatch).
DIST_FIELDS = {"VehicleSpeed", "Odometer", "RatedRange", "IdealBatteryRange", "EstBatteryRange"}
DISTANCE_UNIT = os.environ.get("TLW_DISTANCE_UNIT", "auto").lower()
IMP_CUTOFF = float(os.environ.get("TLW_IMPERIAL_CUTOFF", "0.75"))  # odo < cutoff*known_km -> miles
unit_ref = {}   # vin -> highest odometer seen, in km (detection reference, seeded from DB)
unit_imp = {}   # vin -> True if telemetry currently looks imperial

lock = threading.Lock()
latest = {}   # vin -> {field: value, "_ts": ts}
active = {}   # vin -> {"drive": ts, "charge": ts}
state = {}    # vin -> session dict
car_id = None


def log(*a):
    print(*a, flush=True)


def now():
    return time.time()


def dt3(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def dts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_db = None


def db():
    global _db
    if _db is None:
        _db = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
                              database=DB_NAME, autocommit=True, connect_timeout=10)
    return _db


def execute(sql, params):
    # mariadb drops the idle connection during a long park; the first write then raises a
    # connection error. Reconnect and retry once, quietly — no sample lost, no scary log.
    global _db
    for attempt in (1, 2):
        try:
            with db().cursor() as cur:
                cur.execute(sql, params)
                return cur.lastrowid
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            _db = None  # stale/dropped connection -> get a fresh one on the retry
        except Exception as e:
            log("db error: %s" % e); _db = None; return None
    log("db write failed after reconnect")
    return None


def lv(vin, f):
    return latest.get(vin, {}).get(f)


def is_imperial(vin, L):
    if DISTANCE_UNIT.startswith(("imp", "mi")):
        return True
    if DISTANCE_UNIT.startswith(("met", "km")):
        return False
    if vin in unit_imp:                # auto: odometer-continuity detection (authoritative)
        return unit_imp[vin]
    u = L.get("SettingDistanceUnit")   # bootstrap only, before the first odometer reading
    return "mi" in str(u).lower() if u is not None else False


def detect_unit(vin, raw_odo):
    # Odometer only ever increases by a little between readings; a value well below the known
    # km figure (miles is ~0.62x) means the stream went imperial. Self-corrects when it returns.
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


def power_kw(vin):
    # drive/charge power from pack voltage x current (kW). Sign follows PackCurrent.
    v, i = lv(vin, "PackVoltage"), lv(vin, "PackCurrent")
    if isinstance(v, (int, float)) and isinstance(i, (int, float)):
        return v * i / 1000.0
    return None


def ideal_range(vin):
    return lv(vin, "IdealBatteryRange") if lv(vin, "IdealBatteryRange") is not None else lv(vin, "RatedRange")


def est_range(vin):
    return lv(vin, "EstBatteryRange") if lv(vin, "EstBatteryRange") is not None else lv(vin, "RatedRange")


def as_int(x):
    return int(round(x)) if isinstance(x, (int, float)) else None


def truthy_state(s, *markers):
    if s is None:
        return None
    s = str(s)
    return 1 if any(m in s for m in markers) else 0


def st(vin):
    return state.setdefault(vin, {"mode": None, "drivestate_id": None, "chargingstate_id": None,
                                  "start_charging_id": None, "last_charging_id": None,
                                  "last_pos_id": None, "last_pos_ts": 0, "last_charge_row_ts": 0,
                                  "max_speed": 0, "max_power": 0.0, "start_energy": 0.0,
                                  "charger_type": None, "vstate": None, "state_id": None,
                                  "shift": None, "shift_id": None})


def set_vstate(vin, newstate, ts):
    s = st(vin)
    if s["vstate"] == newstate:
        return
    if s["state_id"]:
        execute("UPDATE state SET EndDate=%s, EndPos=%s WHERE id=%s", (dts(ts), s["last_pos_id"], s["state_id"]))
    s["vstate"] = newstate
    s["state_id"] = execute("INSERT INTO state (StartDate,state,StartPos,CarID) VALUES (%s,%s,%s,%s)",
                            (dts(ts), newstate, s["last_pos_id"], car_id))


def set_shift(vin, gear, ts):
    g = None
    if gear is not None:
        u = str(gear).upper()[-1:]
        if u in ("P", "D", "R", "N"):
            g = u
    s = st(vin)
    if s["shift"] == g:
        return
    if s["shift_id"]:
        execute("UPDATE shiftstate SET EndDate=%s WHERE id=%s", (dts(ts), s["shift_id"]))
    s["shift"] = g
    s["shift_id"] = execute("INSERT INTO shiftstate (StartDate,state,CarID) VALUES (%s,%s,%s)",
                            (dts(ts), g, car_id)) if g else None


# ---- writers --------------------------------------------------------------

def write_pos(vin, ts):
    lat, lng = lv(vin, "Latitude"), lv(vin, "Longitude")
    if lat is None or lng is None:
        return
    p = as_int(power_kw(vin))
    pid = execute(
        """INSERT INTO pos (Datum,lat,lng,speed,power,odometer,ideal_battery_range_km,
           outside_temp,inside_temp,battery_level,sentry_mode,is_preconditioning,
           battery_range_km,CarID) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (dt3(ts), lat, lng, as_int(lv(vin, "VehicleSpeed")), p, lv(vin, "Odometer"),
         ideal_range(vin), lv(vin, "OutsideTemp"), lv(vin, "InsideTemp"), lv(vin, "Soc"),
         truthy_state(lv(vin, "SentryMode"), "Armed", "Aware", "Panic"),
         truthy_state(lv(vin, "HvacPower"), "On"), est_range(vin), car_id))
    if pid:
        s = st(vin)
        s["last_pos_id"] = pid
        s["last_pos_ts"] = ts
        if s["mode"] == "drive" and p is not None:
            s["pmax"] = max(s.get("pmax", p), p)
            s["pmin"] = min(s.get("pmin", p), p)
            s["psum"] = s.get("psum", 0) + p
            s["pcount"] = s.get("pcount", 0) + 1
    return pid


def write_charging_row(vin, ts):
    s = st(vin)
    cid = execute(
        """INSERT INTO charging (battery_level,charge_energy_added,charger_power,Datum,
           ideal_battery_range_km,charger_voltage,outside_temp,battery_range_km,CarID)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (lv(vin, "Soc") or 0, (lv(vin, "ACChargingEnergyIn") or 0) + (lv(vin, "DCChargingEnergyIn") or 0),
         (lv(vin, "ACChargingPower") or 0) + (lv(vin, "DCChargingPower") or 0), dts(ts),
         (ideal_range(vin) or 0), as_int(lv(vin, "ChargerVoltage")), lv(vin, "OutsideTemp"),
         est_range(vin), car_id))
    if cid:
        s["last_charging_id"] = cid; s["last_charge_row_ts"] = ts
    return cid


def open_drive(vin, ts):
    s = st(vin)
    if s["last_pos_id"] is None:
        write_pos(vin, ts)
    if s["last_pos_id"] is None:
        return
    s["max_speed"] = 0
    s["pmax"] = s["pmin"] = s["psum"] = s["pcount"] = 0
    s["drivestate_id"] = execute(
        "INSERT INTO drivestate (StartDate,StartPos,CarID) VALUES (%s,%s,%s)",
        (dts(ts), s["last_pos_id"], car_id))
    log("%s drive start (pos %s)" % (vin, s["last_pos_id"]))


def close_drive(vin, ts):
    s = st(vin)
    if not s["drivestate_id"]:
        return
    write_pos(vin, ts)
    pavg = (s.get("psum", 0) / s["pcount"]) if s.get("pcount") else None
    execute("""UPDATE drivestate SET EndDate=%s, EndPos=%s, speed_max=%s, outside_temp_avg=%s,
               power_max=%s, power_min=%s, power_avg=%s,
               TPMS_FL=%s, TPMS_FR=%s, TPMS_RL=%s, TPMS_RR=%s WHERE id=%s""",
            (dts(ts), s["last_pos_id"], s["max_speed"], lv(vin, "OutsideTemp"),
             s.get("pmax"), s.get("pmin"), pavg,
             lv(vin, "TpmsPressureFl"), lv(vin, "TpmsPressureFr"),
             lv(vin, "TpmsPressureRl"), lv(vin, "TpmsPressureRr"), s["drivestate_id"]))
    log("%s drive end" % vin)
    s["drivestate_id"] = None


def open_charge(vin, ts):
    s = st(vin)
    write_pos(vin, ts)
    s["start_energy"] = (lv(vin, "ACChargingEnergyIn") or 0) + (lv(vin, "DCChargingEnergyIn") or 0)
    s["max_power"] = 0.0
    write_charging_row(vin, ts)
    s["start_charging_id"] = s["last_charging_id"]
    s["chargingstate_id"] = execute(
        "INSERT INTO chargingstate (StartDate,Pos,StartChargingID,CarID,hidden) VALUES (%s,%s,%s,%s,0)",
        (dts(ts), s["last_pos_id"], s["start_charging_id"], car_id))
    log("%s charge start (charging %s)" % (vin, s["start_charging_id"]))


def close_charge(vin, ts):
    s = st(vin)
    if not s["chargingstate_id"]:
        return
    write_charging_row(vin, ts)
    end_energy = (lv(vin, "ACChargingEnergyIn") or 0) + (lv(vin, "DCChargingEnergyIn") or 0)
    added = end_energy - s["start_energy"] if end_energy >= s["start_energy"] else None
    execute("""UPDATE chargingstate SET EndDate=%s, EndChargingID=%s, charge_energy_added=%s,
               max_charger_power=%s, fast_charger_type=%s WHERE id=%s""",
            (dts(ts), s["last_charging_id"], added, as_int(s["max_power"]), s["charger_type"],
             s["chargingstate_id"]))
    log("%s charge end +%skWh" % (vin, round(added, 1) if added else None))
    s["chargingstate_id"] = None


def set_mode(vin, mode, ts):
    s = st(vin)
    if s["mode"] == mode:
        return
    if s["mode"] == "drive":
        close_drive(vin, ts)
    elif s["mode"] == "charge":
        close_charge(vin, ts)
    s["mode"] = mode
    if mode == "drive":
        open_drive(vin, ts)
    elif mode == "charge":
        open_charge(vin, ts)


# ---- mqtt -----------------------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        log("mqtt: connect failed: %s" % reason_code); return
    client.subscribe("%s/+/v/#" % BASE, qos=1)
    log("mqtt: connected, writing teslalogger schema -> %s@%s/%s (CarID=%s)" % (DB_USER, DB_HOST, DB_NAME, car_id))


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
    t = now()
    with lock:
        L = latest.setdefault(vin, {})
        L["_ts"] = t
        active.setdefault(vin, {})
        s = st(vin)
        if field == "SettingDistanceUnit" and L.get("SettingDistanceUnit") != val:
            log("%s SettingDistanceUnit -> %s" % (vin, val))
        if field == "Odometer" and isinstance(val, (int, float)):
            detect_unit(vin, val)   # decide miles vs km from the odometer itself
        if field in DIST_FIELDS and isinstance(val, (int, float)) and is_imperial(vin, L):
            val = val * MI_TO_KM   # normalise miles/(mph) to km/(km-h) for the teslalogger schema
        if field == "Location" and isinstance(val, dict):
            L["Latitude"] = val.get("latitude"); L["Longitude"] = val.get("longitude")
            return
        L[field] = val
        if field == "VehicleSpeed" and isinstance(val, (int, float)) and val > DRIVE_SPEED_MIN:
            active[vin]["drive"] = t
            if val > s["max_speed"]:
                s["max_speed"] = val
        elif field == "Gear" and val is not None and str(val).upper()[-1:] in ("D", "R", "N"):
            active[vin]["drive"] = t
        elif field in ("ACChargingPower", "DCChargingPower") and isinstance(val, (int, float)) and val > CHARGE_POWER_MIN:
            active[vin]["charge"] = t
            p = (L.get("ACChargingPower") or 0) + (L.get("DCChargingPower") or 0)
            if p > s["max_power"]:
                s["max_power"] = p
            s["charger_type"] = "AC" if field == "ACChargingPower" else "DC"


def ticker():
    while True:
        time.sleep(TICK_S)
        t = now()
        with lock:
            for vin in list(latest.keys()):
                L = latest[vin]
                if (t - L.get("_ts", 0)) > ONLINE_TIMEOUT:
                    # offline / asleep: close any open session and mark state
                    ots = L.get("_ts", t)
                    if st(vin)["mode"] is not None:
                        set_mode(vin, None, ots)
                    set_vstate(vin, "asleep", ots)
                    set_shift(vin, None, ots)
                    continue
                a = active.get(vin, {})
                charging = (t - a.get("charge", 0)) < CHARGE_END_TIMEOUT
                moving = (t - a.get("drive", 0)) < DRIVE_END_TIMEOUT
                if charging:
                    set_mode(vin, "charge", t)
                elif moving:
                    set_mode(vin, "drive", t)
                else:
                    set_mode(vin, None, t)
                s = st(vin)
                if s["mode"] == "drive":
                    interval = POS_DRIVE_INTERVAL
                elif s["mode"] == "charge":
                    interval = POS_CHARGE_INTERVAL
                else:
                    interval = POS_IDLE_INTERVAL
                if (t - s["last_pos_ts"]) >= interval:
                    write_pos(vin, t)
                if s["mode"] == "charge" and (t - s["last_charge_row_ts"]) >= CHARGE_ROW_INTERVAL:
                    write_charging_row(vin, t)
                set_vstate(vin, "driving" if s["mode"] == "drive" else
                                "charging" if s["mode"] == "charge" else "online", t)
                set_shift(vin, lv(vin, "Gear"), t)


def main():
    global car_id
    with db().cursor() as c:
        c.execute("SELECT id FROM cars WHERE vin=%s", (VIN,))
        row = c.fetchone()
        car_id = row[0] if row else 1
        # seed the unit-detection reference with the last known (km) odometer
        c.execute("SELECT MAX(odometer) FROM pos WHERE CarID=%s AND odometer>0", (car_id,))
        r2 = c.fetchone()
        if r2 and r2[0]:
            unit_ref[VIN] = float(r2[0]); log("odometer reference: %.0f km" % unit_ref[VIN])
    threading.Thread(target=ticker, daemon=True).start()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tesla-tlwriter")
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as e:
            log("mqtt loop error: %s, retry 10s" % e); time.sleep(10)


if __name__ == "__main__":
    main()
