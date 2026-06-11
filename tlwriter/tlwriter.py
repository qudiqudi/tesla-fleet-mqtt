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
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

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
# fleet-telemetry publishes <base>/<vin>/connectivity CONNECTED/DISCONNECTED; mark asleep this
# long after DISCONNECTED instead of waiting out ONLINE_TIMEOUT of telemetry silence (matches
# teslalogger's quicker sleep detection). Small grace absorbs brief reconnects.
CONN_ASLEEP_GRACE = float(os.environ.get("CONN_ASLEEP_GRACE", "20"))
TICK_S = float(os.environ.get("TICK_S", "10"))
# follows the registered Location streaming cadence (LOCATION_INTERVAL) unless overridden,
# so raising the stream rate in register-telemetry.sh adjusts the pos cadence with it
POS_DRIVE_INTERVAL = float(os.environ.get("POS_DRIVE_INTERVAL", os.environ.get("LOCATION_INTERVAL", "1")))
POS_CHARGE_INTERVAL = float(os.environ.get("POS_CHARGE_INTERVAL", "60"))
POS_IDLE_INTERVAL = float(os.environ.get("POS_IDLE_INTERVAL", "600"))
CHARGE_ROW_INTERVAL = float(os.environ.get("CHARGE_ROW_INTERVAL", "60"))
REPLAY_GRACE = float(os.environ.get("REPLAY_GRACE", "10"))  # ignore retained replay for Ns after connect

# Home Assistant: optionally publish derived/normalised state to <BASE>/<VIN>/ha/<name>
# (retained) for the HA discovery service to point entities at. tlwriter is the right place:
# it already holds the authoritative session state and the km-normalised values, so HA stays
# consistent with Grafana. Off by default so the parallel validator is unaffected.
HA_PUBLISH = os.environ.get("HA_PUBLISH", "0").lower() in ("1", "true", "yes")
# General geofencing is left to Home Assistant (HA's own zones resolve work/etc. off the GPS we
# publish). The one exception is a single optional HOME zone, because the address columns the
# teslalogger trip view shows are written here, not in HA -- without it a drive from home reverse-
# geocodes to a bare street address (or nothing). Set HOME_LAT/HOME_LNG to name positions within
# HOME_RADIUS metres HOME_LABEL instead of geocoding them. Unset -> no home zone (plain geocoding).
HOME_LABEL = os.environ.get("HOME_LABEL", "Home")
HOME_RADIUS = float(os.environ.get("HOME_RADIUS", "50"))  # metres
try:
    HOME = (float(os.environ["HOME_LAT"]), float(os.environ["HOME_LNG"]))
except (KeyError, ValueError):
    HOME = None

# Reverse-geocode each drive's start/end into pos.address (the teslalogger `trip` view reads
# pos_start.address / pos_end.address). Off-thread, throttled to OSM Nominatim's 1 req/s policy
# with an identifying User-Agent; failures are logged and skipped so they never block logging.
GEOCODE = os.environ.get("TLW_GEOCODE", "1").lower() in ("1", "true", "yes")
NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
GEOCODE_UA = os.environ.get("GEOCODE_USER_AGENT",
                            "tesla-fleet-mqtt/1.0 (https://github.com/qudiqudi/tesla-fleet-mqtt)")
GEOCODE_MIN_INTERVAL = float(os.environ.get("GEOCODE_MIN_INTERVAL", "1.1"))  # >=1s per OSM policy
GEOCODE_BACKFILL_LIMIT = int(os.environ.get("GEOCODE_BACKFILL_LIMIT", "200"))

# Name charge stops after the charging operator (like teslalogger's charger geofences) instead of
# a bare street address: on charge start the charge position is looked up against Open Charge Map
# (if OCM_API_KEY is set -- free key from openchargemap.org) and then OSM's amenity=charging_station
# via Overpass, falling back to the street address when neither knows the spot. Home charging stays
# "Home" (the home zone wins first). TLW_CHARGER_NAMES=0 disables.
CHARGER_NAMES = os.environ.get("TLW_CHARGER_NAMES", "1").lower() in ("1", "true", "yes")
CHARGER_RADIUS = float(os.environ.get("CHARGER_RADIUS", "75"))  # metres around the charge position
OCM_API_KEY = os.environ.get("OCM_API_KEY", "")
OCM_API_URL = os.environ.get("OCM_API_URL", "https://api.openchargemap.io/v3/poi/")
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")

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

# Power/speed stream null when inactive; that null IS the live value (0), not a gap. Keeping
# the last non-null value instead left e.g. a stale DCChargingPower from a past supercharge
# in latest[], inflating every later AC session's power sum.
NULL_ZERO_FIELDS = {"VehicleSpeed", "ACChargingPower", "DCChargingPower"}
# Fields consumed as numbers below. Some firmwares marshal numerics as JSON strings ("4.5");
# coerce here, and never store a non-numeric value that would crash the tick arithmetic later.
NUMERIC_FIELDS = {"VehicleSpeed", "Odometer", "Soc", "BatteryLevel", "RatedRange", "IdealBatteryRange",
                  "EstBatteryRange", "OutsideTemp", "InsideTemp", "ACChargingPower",
                  "DCChargingPower", "ACChargingEnergyIn", "DCChargingEnergyIn",
                  "ChargerVoltage", "PackVoltage", "PackCurrent", "ModuleTempMin",
                  "ModuleTempMax", "EnergyRemaining", "ChargeRateMilePerHour",
                  "TpmsPressureFl", "TpmsPressureFr", "TpmsPressureRl", "TpmsPressureRr",
                  "SoftwareUpdateDownloadPercentComplete",
                  "SoftwareUpdateInstallationPercentComplete", "GpsHeading"}
unit_ref = {}   # vin -> highest odometer seen, in km (detection reference, seeded from DB)
unit_imp = {}   # vin -> True if telemetry currently looks imperial

lock = threading.Lock()
latest = {}   # vin -> {field: value, "_ts": ts}
active = {}   # vin -> {"drive": ts, "charge": ts}
state = {}    # vin -> session dict
conn = {}     # vin -> (status, ts): "CONNECTED"/"DISCONNECTED" from the connectivity topic
_last_version = {}     # vin -> last firmware written to car_version
_last_tpms = {}        # (vin, tireid) -> last pressure written to TPMS
TPMS_TIRE = {"TpmsPressureFl": 1, "TpmsPressureFr": 2, "TpmsPressureRl": 3, "TpmsPressureRr": 4}
car_id = None
mqtt_client = None    # set in main(); used to publish HA topics
_ha_last = {}         # name -> last published payload (publish only on change)


def log(*a):
    print(*a, flush=True)


def now():
    return time.time()


# teslalogger's schema stores wall-clock LOCAL time (its dashboards read the columns without any
# tz conversion), and this DB is a copy of a teslalogger DB, so tlwriter must match: write local
# time, not UTC. The Grafana MySQL datasource is set to the same local zone (SYSTEM) so the values
# render as-is. ts is a UTC epoch; render it in LOCAL_TZ. DST is handled by the zone. A bad TLW_TZ
# or missing tzdata must NOT crash the whole writer -- fall back to UTC (the pre-local behaviour).
try:
    LOCAL_TZ = ZoneInfo(os.environ.get("TLW_TZ", "Europe/Berlin"))
except Exception as _tz_err:
    print("TLW_TZ load failed (%r); falling back to UTC" % _tz_err, flush=True)
    LOCAL_TZ = timezone.utc


def dt3(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def dts(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


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


# ---- reverse geocoding ----------------------------------------------------
# The worker runs on its own thread + DB connection (pymysql isn't thread-safe, and the OSM
# call can take seconds — must not touch the main connection or hold up MQTT processing).
geocode_q = queue.Queue()
_geo_db = None
_geo_last = 0.0


def _geo_conn():
    global _geo_db
    if _geo_db is None:
        _geo_db = pymysql.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
                                  database=DB_NAME, autocommit=True, connect_timeout=10)
    return _geo_db


def home_dist_m(lat, lng):
    # equirectangular approximation -- accurate to cm at geofence range, no need for haversine
    dx = math.radians(lng - HOME[1]) * math.cos(math.radians((lat + HOME[0]) / 2))
    dy = math.radians(lat - HOME[0])
    return 6371000.0 * math.hypot(dx, dy)


def geocode_address(lat, lng):
    params = urllib.parse.urlencode({"lat": "%.6f" % lat, "lon": "%.6f" % lng,
                                     "format": "jsonv2", "zoom": "18", "addressdetails": "1"})
    req = urllib.request.Request(NOMINATIM_URL + "?" + params, headers={"User-Agent": GEOCODE_UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8", "replace")) or {}
    a = d.get("address", {})
    city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county") or ""
    left = ("%s %s" % (a.get("postcode", ""), city)).strip()
    right = ("%s %s" % (a.get("road", ""), a.get("house_number", ""))).strip()
    addr = ", ".join(p for p in (left, right) if p)
    # never return empty: a sparse rural result still has a display_name, and a coordinate string
    # is better than a blank trip column (the dashboard hides blanks; "empty is not possible").
    addr = addr or d.get("display_name") or "%.5f, %.5f" % (lat, lng)
    return addr[:255]


def _charger_label(operator, place):
    # "<operator>, <road/town>" -- keep it short and teslalogger-ish; either part may be missing
    operator, place = (operator or "").strip(), (place or "").strip()
    if operator and place and place.lower() not in operator.lower():
        return ("%s, %s" % (operator, place))[:255]
    return (operator or place)[:255] or None


def _fetch_json(req, timeout):
    # Charger sources (OCM, Overpass) rate-limit (429) and overload (5xx) in bursts; retry those
    # and transient network errors with a short backoff so one blip doesn't drop the name.
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504) and i < 2:
                time.sleep(4 * (i + 1) ** 2)   # 4s, 16s
                continue
            raise
        except urllib.error.URLError:
            if i < 2:
                time.sleep(4 * (i + 1))
                continue
            raise
    return None


def _select_charger(cands, brand):
    # cands: nearest-first list of (operator, place). When the car reports the charger brand, pick
    # the matching operator instead of the closest -- so a Supercharger next to an Ionity is named
    # for whichever the car actually used (brand "Tesla" -> the Tesla operator; anything else, a
    # third-party CCS -> the non-Tesla operator). Unknown brand keeps the nearest.
    cands = [(o, p) for o, p in cands if o]
    if not cands:
        return None
    b = (brand or "").lower()
    if "tesla" in b:                                   # Supercharge
        for o, p in cands:
            if "tesla" in o.lower():
                return _charger_label(o, p)
        return _charger_label("Tesla Supercharger", cands[0][1])   # the SuC isn't mapped here, but we know
    if b:                                              # a known, non-Tesla brand ("<invalid>" = CCS)
        for o, p in cands:
            if "tesla" not in o.lower():
                return _charger_label(o, p)
    return _charger_label(*cands[0])                   # unknown brand, or only Tesla mapped -> nearest


def ocm_candidates(lat, lng):
    # Open Charge Map: best operator names, but needs a (free) API key. Nearest-first (lat/lng query).
    if not OCM_API_KEY:
        return []
    params = urllib.parse.urlencode({"output": "json", "latitude": "%.6f" % lat, "longitude": "%.6f" % lng,
                                     "distance": CHARGER_RADIUS / 1000.0, "distanceunit": "KM",
                                     "maxresults": "5", "key": OCM_API_KEY})
    req = urllib.request.Request(OCM_API_URL + "?" + params, headers={"User-Agent": GEOCODE_UA})
    out = []
    for poi in _fetch_json(req, 15) or []:
        op = (poi.get("OperatorInfo") or {}).get("Title") or ""
        ai = poi.get("AddressInfo") or {}
        if op.lower() in ("", "(unknown operator)", "unknown"):
            op = ai.get("Title") or ""   # fall back to the station's own title
        out.append((op, ai.get("AddressLine1") or ai.get("Town")))
    return out


def osm_candidates(lat, lng):
    # OSM amenity=charging_station via Overpass: no key, reuses the data behind our geocoding
    q = ("[out:json][timeout:25];nwr(around:%d,%.6f,%.6f)[amenity=charging_station];out tags center;"
         % (int(CHARGER_RADIUS), lat, lng))
    req = urllib.request.Request(OVERPASS_URL, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": GEOCODE_UA})
    out = []
    for e in (_fetch_json(req, 30) or {}).get("elements", []):
        t = e.get("tags", {})
        op = t.get("operator") or t.get("brand") or t.get("network") or t.get("name")
        if op:
            out.append((op, t.get("addr:street")))
    return out


def charger_name(lat, lng, brand=None):
    # operator name for a charge stop, OCM first then OSM; None if neither knows it (caller geocodes)
    for fn in (ocm_candidates, osm_candidates):
        try:
            name = _select_charger(fn(lat, lng), brand)
            if name:
                return name
        except Exception as e:
            log("charger lookup (%s): %s" % (fn.__name__, e))
    return None


def nearby_charger_name(lat, lng):
    # A drive start/end that sits on a charge stop we've already named should show that operator,
    # like teslalogger's geofence (which covered the drive ends at a charger too). Reuse the nearest
    # named charge stop within CHARGER_RADIUS -- one cheap query over the small chargingstate set, no
    # API call, no brand needed (the charge that named it already picked the right operator). Street
    # addresses and the home label aren't inherited (the latter only belongs inside the home zone).
    if not CHARGER_NAMES:
        return None
    with _geo_conn().cursor() as cur:
        cur.execute(
            "SELECT cp.address FROM chargingstate cs JOIN pos cp ON cp.id=cs.Pos"
            " WHERE cs.CarID=%s AND cp.lat IS NOT NULL AND cp.address IS NOT NULL AND cp.address<>''"
            " AND cp.address<>%s AND cp.address NOT REGEXP '^[0-9]{5} ' AND cp.address NOT REGEXP '^[a-z]{2}-'"
            " AND ST_Distance_Sphere(POINT(cp.lng,cp.lat),POINT(%s,%s))<=%s"
            " ORDER BY ST_Distance_Sphere(POINT(cp.lng,cp.lat),POINT(%s,%s)) LIMIT 1",
            (car_id, HOME_LABEL, lng, lat, CHARGER_RADIUS, lng, lat))
        row = cur.fetchone()
    return row[0] if row else None


def geocode_worker():
    global _geo_db, _geo_last
    backfill_geocode()   # one-shot at startup, in this thread (only thread touching the geo conn)
    while True:
        pos_id, is_charger, brand = geocode_q.get()
        try:
            # Read the position's OWN coordinates rather than trusting live lat/lng at queue time:
            # a drive opens off an older idle pos, and right after wake the live Location can be
            # stale/absent -> the start would silently never get named (the end always did because
            # close_drive writes a fresh pos first). The pos row always has coords, so this is reliable.
            with _geo_conn().cursor() as cur:
                cur.execute("SELECT lat, lng FROM pos WHERE id=%s AND (address IS NULL OR address='')", (pos_id,))
                row = cur.fetchone()
            if not row or row[0] is None or row[1] is None:
                continue   # gone, already named, or no GPS fix on that row
            lat, lng = float(row[0]), float(row[1])
            if lat == 0 and lng == 0:
                continue   # 0,0 is a no-fix glitch, not a place -- don't label it "0.00000, 0.00000"
            if HOME and home_dist_m(lat, lng) <= HOME_RADIUS:
                addr = HOME_LABEL   # home wins first, so home AC charging stays "Home" (no lookup)
            # a drive endpoint (not a charge stop) sitting on a named charge stop reuses it; computed
            # lazily here so home positions and charge stops never pay for the query
            elif not (is_charger and CHARGER_NAMES) and (inherited := nearby_charger_name(lat, lng)):
                addr = inherited
            else:
                wait = GEOCODE_MIN_INTERVAL - (now() - _geo_last)
                if wait > 0:
                    time.sleep(wait)
                _geo_last = now()   # mark the attempt up front so failures are throttled too
                # charge stops: name them after the operator (OCM/OSM), else fall back to the address
                addr = (charger_name(lat, lng, brand) if is_charger and CHARGER_NAMES else None) \
                    or geocode_address(lat, lng)
            if addr:
                with _geo_conn().cursor() as cur:
                    cur.execute("UPDATE pos SET address=%s WHERE id=%s AND (address IS NULL OR address='')",
                                (addr, pos_id))
        except (pymysql.err.OperationalError, pymysql.err.InterfaceError):
            _geo_db = None  # drop the stale connection; reconnect on the next item
        except Exception as e:
            log("geocode: pos %s: %s" % (pos_id, e))
        finally:
            geocode_q.task_done()


def queue_geocode(vin, pos_id, charger=False, brand=None):
    if not GEOCODE or pos_id is None:
        return
    geocode_q.put((pos_id, charger, brand))   # the worker reads the row's own coords -- see geocode_worker


def backfill_geocode():
    # Geocode existing drive start/end positions that have no address yet (this writer's own
    # drives, which teslalogger never geocoded). Runs in the worker thread so it can't block
    # startup. Two cheap PK-indexed steps — NOT a `pos JOIN drivestate ON p.id IN (...)`, which
    # is a non-indexed cross scan that pegs the DB on a large pos table.
    if not GEOCODE:
        return
    try:
        with _geo_conn().cursor() as cur:
            cur.execute("SELECT StartPos, EndPos FROM drivestate WHERE CarID=%s ORDER BY id DESC LIMIT 1000",
                        (car_id,))
            ids = {p for row in cur.fetchall() for p in row if p}
            if not ids:
                return
            placeholders = ",".join(["%s"] * len(ids))
            # build the IN list by concatenation (NOT %-format), so the LIMIT %s stays a pymysql param
            cur.execute("SELECT id FROM pos WHERE id IN (" + placeholders + ") "
                        "AND lat IS NOT NULL AND (address IS NULL OR address='') "
                        "ORDER BY id DESC LIMIT %s", tuple(ids) + (GEOCODE_BACKFILL_LIMIT,))
            rows = cur.fetchall()
        for (pid,) in rows:
            geocode_q.put((pid, False, None))   # the worker reads each row's own coords (drive ends, not chargers)
        if rows:
            log("geocode: backfilling %d drive positions" % len(rows))
    except Exception as e:
        log("geocode backfill: %s" % e)


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


def soc_disp(vin):
    # displayed battery percent: teslalogger's battery_level holds the car-display value,
    # which fleet telemetry streams as BatteryLevel; Soc is the (slightly lower) usable SoC
    v = lv(vin, "BatteryLevel")
    return v if v is not None else lv(vin, "Soc")


def _range_or_rated(vin, primary):
    v = lv(vin, primary)
    return v if v is not None else lv(vin, "RatedRange")


def ideal_range(vin):
    return _range_or_rated(vin, "IdealBatteryRange")


def cell_temp(vin):
    # representative battery "cell" temperature from the module min/max (teslalogger's Cell
    # Temperature panel reads it from can id=2)
    vals = [x for x in (lv(vin, "ModuleTempMin"), lv(vin, "ModuleTempMax")) if isinstance(x, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None


def est_range(vin):
    return _range_or_rated(vin, "EstBatteryRange")


def as_int(x):
    return int(round(x)) if isinstance(x, (int, float)) else None


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
                                  "charger_type": None, "charge_pos_id": None,
                                  "vstate": None, "state_id": None,
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
    g = gear_letter(gear)
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
         ideal_range(vin), lv(vin, "OutsideTemp"), lv(vin, "InsideTemp"), soc_disp(vin),
         truthy_state(lv(vin, "SentryMode"), "Armed", "Aware", "Panic"),
         truthy_state(lv(vin, "HvacPower"), "On"), est_range(vin), car_id))
    s = st(vin)
    ct = cell_temp(vin)   # write a can row only when the temp changes (pos cadence can be 1s)
    if ct is not None and ct != s.get("last_cell_temp"):
        execute("INSERT INTO can (datum,id,val,CarID) VALUES (%s,2,%s,%s)", (dts(ts), ct, car_id))
        s["last_cell_temp"] = ct
    if pid:
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
        (soc_disp(vin), (lv(vin, "ACChargingEnergyIn") or 0) + (lv(vin, "DCChargingEnergyIn") or 0),
         (lv(vin, "ACChargingPower") or 0) + (lv(vin, "DCChargingPower") or 0), dts(ts),
         (ideal_range(vin) or 0), as_int(lv(vin, "ChargerVoltage")), lv(vin, "OutsideTemp"),
         est_range(vin), car_id))
    if cid:
        s["last_charging_id"] = cid; s["last_charge_row_ts"] = ts
    return cid


def write_car_version(vin, version, ts):
    # teslalogger's Firmware panel reads car_version; tlwriter must keep it current. On change only.
    # Compare/store the same normalised string, and _last_version is seeded from the DB at startup
    # (main()) -- otherwise every restart re-inserts the unchanged firmware (the in-memory dict is
    # empty on boot), which during a deploy storm piles up duplicate rows.
    if not version:
        return
    v = str(version)[:50]
    if _last_version.get(vin) == v:
        return
    _last_version[vin] = v
    execute("INSERT INTO car_version (StartDate, version, CarID) VALUES (%s,%s,%s)", (dts(ts), v, car_id))
    log("%s firmware -> %s" % (vin, v))


def write_tpms(vin, field, pressure, ts):
    # teslalogger's TPMS panels read the TPMS table (one row per tire). On change only.
    tire = TPMS_TIRE.get(field)
    if tire is None or not isinstance(pressure, (int, float)):
        return
    key = (vin, tire)
    if _last_tpms.get(key) == pressure:
        return
    _last_tpms[key] = pressure
    execute("INSERT IGNORE INTO TPMS (CarId, Datum, TireId, Pressure) VALUES (%s,%s,%s,%s)",
            (car_id, dts(ts), tire, pressure))


def open_drive(vin, ts):
    s = st(vin)
    if s["last_pos_id"] is None:
        write_pos(vin, ts)
    if s["last_pos_id"] is None:
        return
    s["max_speed"] = 0
    s["pmax"] = s["pmin"] = s["psum"] = s["pcount"] = 0
    # trip baseline for the HA trip_* entities (distance/energy deltas since the drive started)
    s["trip_start_ts"] = ts
    s["trip_start_odo"] = lv(vin, "Odometer")
    s["trip_start_er"] = lv(vin, "EnergyRemaining")
    s["drivestate_id"] = execute(
        "INSERT INTO drivestate (StartDate,StartPos,CarID) VALUES (%s,%s,%s)",
        (dts(ts), s["last_pos_id"], car_id))
    queue_geocode(vin, s["last_pos_id"])   # name the start of the trip
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
    queue_geocode(vin, s["last_pos_id"])   # name the end of the trip (EndPos)
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
    s["charge_pos_id"] = s["last_pos_id"]   # named at close_charge, once the charger brand is known
    log("%s charge start (charging %s)" % (vin, s["start_charging_id"]))


def close_charge(vin, ts):
    s = st(vin)
    if not s["chargingstate_id"]:
        return
    write_charging_row(vin, ts)
    end_energy = (lv(vin, "ACChargingEnergyIn") or 0) + (lv(vin, "DCChargingEnergyIn") or 0)
    added = end_energy - s["start_energy"] if end_energy >= s["start_energy"] else None
    # Fleet telemetry has no charger-brand field; the brand lives in FastChargerType, whose enum
    # distinguishes "Supercharger" from the CCS types (Combo/CHAdeMO/...). Normalise to teslalogger's
    # fast_charger_brand values ("Tesla"/"<invalid>") so history and the lookup agree. Only trust it
    # for DC -- gating avoids a stale Supercharger type bleeding into a later AC charge (latest[] keeps
    # the last value).
    brand = None
    if s.get("charger_type") == "DC":
        brand = "Tesla" if "supercharger" in (lv(vin, "FastChargerType") or "").lower() else "<invalid>"
    execute("""UPDATE chargingstate SET EndDate=%s, EndChargingID=%s, charge_energy_added=%s,
               max_charger_power=%s, fast_charger_type=%s, fast_charger_brand=%s WHERE id=%s""",
            (dts(ts), s["last_charging_id"], added, as_int(s["max_power"]), s["charger_type"],
             brand or None, s["chargingstate_id"]))
    # name the stop now -- the brand (Tesla vs a third-party CCS) is known, so a Supercharger next
    # to an Ionity gets the operator the car actually used, not just the nearest station
    queue_geocode(vin, s.get("charge_pos_id"), charger=True, brand=brand)
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

_connect_ts = 0.0


def on_connect(client, userdata, flags, reason_code, properties=None):
    global _connect_ts
    if reason_code != 0:
        log("mqtt: connect failed: %s" % reason_code); return
    _connect_ts = now()
    client.subscribe("%s/+/v/#" % BASE, qos=1)
    client.subscribe("%s/+/connectivity" % BASE, qos=1)
    log("mqtt: connected, writing teslalogger schema -> %s@%s/%s (CarID=%s)" % (DB_USER, DB_HOST, DB_NAME, car_id))


def on_message(client, userdata, msg):
    parts = msg.topic.split("/")
    if parts[-1] == "connectivity":   # explicit online/offline signal -> faster sleep detection
        try:
            status = (json.loads(msg.payload.decode("utf-8", "replace")) or {}).get("Status")
        except json.JSONDecodeError:
            return
        if status:
            with lock:
                conn[parts[-2]] = (status, now())
        return
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
        if field in NUMERIC_FIELDS and not isinstance(val, (int, float)):
            try:
                val = float(val)
            except (TypeError, ValueError):
                return   # garbage in a numeric field: never store it
    t = now()
    # The publisher retains every message, so the broker REPLAYS the last value of each topic
    # on (re)subscribe. That replay (a burst right after connect) must not count as liveness, or
    # an asleep car looks online. A retained message arriving later is just normal live data.
    replay = bool(getattr(msg, "retain", False)) and (t - _connect_ts) < REPLAY_GRACE
    with lock:
        L = latest.setdefault(vin, {})
        if not replay:
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
        if field == "Version":
            write_car_version(vin, val, t)    # keep car_version live (also captures the current one on replay)
        elif field in TPMS_TIRE:
            write_tpms(vin, field, val, t)    # keep the TPMS table live
        if replay:   # value kept for last-known lookups, but no drive/charge activity from a replay
            return
        if field == "VehicleSpeed" and isinstance(val, (int, float)) and val > DRIVE_SPEED_MIN:
            active[vin]["drive"] = t
            if val > s["max_speed"]:
                s["max_speed"] = val
        elif field == "Gear" and gear_letter(val) in ("D", "R", "N"):
            active[vin]["drive"] = t
        elif field in ("ACChargingPower", "DCChargingPower") and isinstance(val, (int, float)) and val > CHARGE_POWER_MIN:
            active[vin]["charge"] = t
            p = (L.get("ACChargingPower") or 0) + (L.get("DCChargingPower") or 0)
            if p > s["max_power"]:
                s["max_power"] = p
            s["charger_type"] = "AC" if field == "ACChargingPower" else "DC"


def ha_pub(name, value):
    # Publish a derived value to <BASE>/<VIN>/ha/<name>, retained, only when it changed.
    if value is None or mqtt_client is None:
        return
    if isinstance(value, bool):
        payload = "true" if value else "false"
    elif isinstance(value, float):
        payload = ("%.3f" % value).rstrip("0").rstrip(".")
    else:
        payload = str(value)
    if _ha_last.get(name) == payload:
        return
    _ha_last[name] = payload
    mqtt_client.publish("%s/%s/ha/%s" % (BASE, VIN, name), payload, qos=1, retain=True)


def publish_ha(vin):
    # Derived/normalised live state for Home Assistant. Mirrors the authoritative session
    # state (set_vstate/set_mode) and the km-normalised latest values, so the HA dashboard
    # agrees with Grafana. Called each tick; ha_pub() suppresses unchanged values.
    if not HA_PUBLISH:
        return
    s = st(vin)
    vstate = s["vstate"]
    mode = s["mode"]
    # the DB state table is restricted to teslalogger's vocabulary; HA keeps the richer view
    ha_pub("state", "driving" if mode == "drive" else "charging" if mode == "charge" else vstate)
    ha_pub("sleeping", vstate == "asleep")
    ha_pub("online", vstate == "online")
    ha_pub("driving", mode == "drive")
    ha_pub("charging", mode == "charge")

    # Several fields stream null when inactive (VehicleSpeed/charge power parked, Gear, ...) and
    # on_message keeps the last non-null value to protect the DB writer. For the live HA view that
    # would show stale speed/power on a parked car, so gate the transient ones on the session mode.
    charging = mode == "charge"
    driving = mode == "drive"
    plugged = charging or bool(lv(vin, "ChargePortDoorOpen"))
    ha_pub("plugged_in", plugged)
    ha_pub("fast_charger", charging and (lv(vin, "DCChargingPower") or 0) > CHARGE_POWER_MIN)

    # distance/speed/range are already km-normalised in latest[] (see on_message)
    ha_pub("odometer_km", round(lv(vin, "Odometer"), 1) if lv(vin, "Odometer") is not None else None)
    spd = lv(vin, "VehicleSpeed")
    ha_pub("speed_kmh", round(spd) if (driving and isinstance(spd, (int, float))) else 0)
    ha_pub("battery_range_km", round(lv(vin, "RatedRange"), 1) if lv(vin, "RatedRange") is not None else None)
    ir = ideal_range(vin)
    ha_pub("ideal_range_km", round(ir, 1) if isinstance(ir, (int, float)) else None)
    crm = lv(vin, "ChargeRateMilePerHour") if charging else 0   # field name is always miles/h
    ha_pub("charge_rate_km", round(crm * MI_TO_KM, 1) if isinstance(crm, (int, float)) else 0)

    power = ((lv(vin, "ACChargingPower") or 0) + (lv(vin, "DCChargingPower") or 0)) if charging else 0
    ha_pub("charger_power_kw", round(power, 1))
    total_e = (lv(vin, "ACChargingEnergyIn") or 0) + (lv(vin, "DCChargingEnergyIn") or 0)
    if mode == "charge":
        s["ha_energy_added"] = max(0.0, total_e - s.get("start_energy", 0.0))
    ha_pub("energy_added_kwh", round(s.get("ha_energy_added", 0.0), 2))

    doors = lv(vin, "DoorState")
    if isinstance(doors, dict):
        ha_pub("open_doors", sum(1 for k in ("DriverFront", "DriverRear", "PassengerFront", "PassengerRear")
                                 if doors.get(k)))
        ha_pub("frunk", bool(doors.get("TrunkFront")))
        ha_pub("trunk", bool(doors.get("TrunkRear")))

    lat, lng = lv(vin, "Latitude"), lv(vin, "Longitude")
    if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
        # GPS for the device_tracker; HA's own zones resolve home/work/etc. from these coords.
        ha_pub("gps", json.dumps({"latitude": lat, "longitude": lng,
                                  "gps_accuracy": 5, "source_type": "gps"}))

    # windows: open count from the four window fields (enum string; anything but Closed = open)
    wins = [lv(vin, w) for w in ("FdWindow", "FpWindow", "RdWindow", "RpWindow")]
    if any(w is not None for w in wins):
        ha_pub("open_windows", sum(1 for w in wins if w is not None and "Closed" not in str(w)))

    # firmware update status, derived from the update progress fields
    dl = lv(vin, "SoftwareUpdateDownloadPercentComplete")
    inst = lv(vin, "SoftwareUpdateInstallationPercentComplete")
    upd_ver = lv(vin, "SoftwareUpdateVersion")
    if isinstance(inst, (int, float)) and inst > 0:
        ha_pub("software_update_status", "installing")
    elif isinstance(dl, (int, float)) and dl > 0:
        ha_pub("software_update_status", "downloading")
    elif upd_ver not in (None, ""):
        ha_pub("software_update_status", "available")
    else:
        ha_pub("software_update_status", "")

    # trip_* metrics: only recompute while driving; when the drive ends the last values persist
    if mode == "drive":
        ha_pub("trip_max_speed", round(s["max_speed"]) if s.get("max_speed") else 0)
        ha_pub("trip_max_power", round(s["pmax"], 1) if s.get("pmax") is not None else 0)
        start_ts = s.get("trip_start_ts")
        if start_ts:
            ha_pub("trip_duration_sec", int(now() - start_ts))
            iso = datetime.fromtimestamp(start_ts, LOCAL_TZ).isoformat()   # tz-aware, local -- matches the DB
            ha_pub("trip_start_dt", iso)
            ha_pub("trip_start", datetime.fromtimestamp(start_ts, LOCAL_TZ).strftime("%Y-%m-%d %H:%M"))
        odo, odo0 = lv(vin, "Odometer"), s.get("trip_start_odo")
        dist = None
        if isinstance(odo, (int, float)) and isinstance(odo0, (int, float)):
            dist = max(0.0, odo - odo0)
            ha_pub("trip_distance", round(dist, 1))
        er, er0 = lv(vin, "EnergyRemaining"), s.get("trip_start_er")
        if isinstance(er, (int, float)) and isinstance(er0, (int, float)):
            kwh = max(0.0, er0 - er)
            ha_pub("trip_kwh", round(kwh, 2))
            if dist and dist > 0.1:
                ha_pub("trip_avg_kwh", round(kwh * 1000.0 / dist, 1))   # Wh/km


def tick_vin(vin, t):
    L = latest[vin]
    cs, cts = conn.get(vin, (None, 0))
    # Live telemetry vetoes the DISCONNECTED signal: fleet-telemetry can emit a stale/
    # out-of-order DISCONNECTED for a parallel connection while the car streams on (seen
    # 2026-06-10: a mid-drive flap marked the car asleep, truncated the drive and dropped
    # 13 min of pos rows). When the car really sleeps, telemetry stops with the disconnect,
    # so requiring both keeps the fast sleep detection.
    silent = (t - L.get("_ts", 0)) > CONN_ASLEEP_GRACE
    disc = cs == "DISCONNECTED" and (t - cts) > CONN_ASLEEP_GRACE and silent
    if (t - L.get("_ts", 0)) > ONLINE_TIMEOUT or disc:
        # offline / asleep: close any open session and mark state (connectivity catches
        # it sooner than waiting out the telemetry-silence timeout)
        ots = L.get("_ts", t)
        if st(vin)["mode"] is not None:
            set_mode(vin, None, ots)
        set_vstate(vin, "asleep", ots)
        set_shift(vin, None, ots)
        publish_ha(vin)
        return
    a = active.get(vin, {})
    charging = (t - a.get("charge", 0)) < CHARGE_END_TIMEOUT
    moving = (t - a.get("drive", 0)) < DRIVE_END_TIMEOUT
    if charging:
        set_mode(vin, "charge", t)
    elif moving:
        set_mode(vin, "drive", t)
    else:
        # close at the LAST ACTIVITY, not at timeout expiry: teslalogger ends a drive when
        # the car parks, not DRIVE_END_TIMEOUT later (EndDate and trip duration match)
        s0 = st(vin)
        end_ts = a.get("drive" if s0["mode"] == "drive" else "charge", t) if s0["mode"] else t
        set_mode(vin, None, end_ts)
    s = st(vin)
    # a session that opened without a GPS fix (or with the DB down) has no opening row yet;
    # set_mode() won't re-enter the same mode, so retry the open here until it sticks —
    # otherwise the whole drive/charge would silently go unlogged
    if s["mode"] == "drive" and s["drivestate_id"] is None:
        open_drive(vin, t)
    elif s["mode"] == "charge" and s["chargingstate_id"] is None:
        open_charge(vin, t)
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
    # teslalogger's state table only ever holds online/asleep/offline/waking — its dashboards
    # map anything else to N/A and derive Driving/Charging from the trip/chargingstate tables
    set_vstate(vin, "online", t)
    set_shift(vin, lv(vin, "Gear"), t)
    publish_ha(vin)


def ticker():
    while True:
        time.sleep(TICK_S)
        t = now()
        with lock:
            for vin in list(latest.keys()):
                try:
                    tick_vin(vin, t)
                except Exception as e:
                    # one bad value/tick must not kill the thread — it's the only thing
                    # writing pos rows and opening/closing sessions
                    log("tick %s: %r" % (vin, e))


def resume_sessions():
    # On restart, CONTINUE the open sessions a previous run left, instead of closing them and
    # opening new ones. A CI/CD redeploy recreates the container; orphaning+reseeding on every
    # recreate piled up overlapping rows and dirtied the data. Here we adopt the latest open row
    # per table into memory and close only extra/older opens; inverted rows fixed defensively.
    s = st(VIN)
    with db().cursor() as c:
        def adopt(tbl, col, id_key, val_key):
            c.execute("SELECT id, %s FROM %s WHERE CarID=%%s AND EndDate IS NULL ORDER BY StartDate DESC"
                      % (col, tbl), (car_id,))
            rows = c.fetchall()
            if rows:
                s[id_key] = rows[0][0]
                if val_key:
                    s[val_key] = rows[0][1]
                for r in rows[1:]:   # close stray older open rows from past bugs
                    c.execute("UPDATE %s SET EndDate=StartDate WHERE id=%%s" % tbl, (r[0],))
            return bool(rows)
        adopt("state", "state", "state_id", "vstate")
        adopt("shiftstate", "state", "shift_id", "shift")
        if adopt("drivestate", "StartPos", "drivestate_id", None):
            s["mode"] = "drive"
        if adopt("chargingstate", "StartChargingID", "chargingstate_id", "start_charging_id") and s["mode"] is None:
            s["mode"] = "charge"
        if s["start_charging_id"]:
            # restore the session's energy baseline from the opening charging row (its
            # charge_energy_added holds the cumulative counter at charge start); without it
            # close_charge() would record the car's whole lifetime energy counter as this
            # session's charge_energy_added
            c.execute("SELECT charge_energy_added FROM charging WHERE id=%s", (s["start_charging_id"],))
            r = c.fetchone()
            if r and r[0] is not None:
                s["start_energy"] = float(r[0])
        for tbl in ("state", "shiftstate", "drivestate", "chargingstate"):
            c.execute("UPDATE %s SET EndDate=StartDate WHERE CarID=%%s AND EndDate IS NOT NULL "
                      "AND EndDate<StartDate" % tbl, (car_id,))
    if s["vstate"]:
        log("resumed: state=%s mode=%s" % (s["vstate"], s["mode"]))


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
        # seed last-known firmware so a restart doesn't re-insert the unchanged version
        c.execute("SELECT version FROM car_version WHERE CarID=%s ORDER BY id DESC LIMIT 1", (car_id,))
        r3 = c.fetchone()
        if r3 and r3[0]:
            _last_version[VIN] = r3[0]
    resume_sessions()   # continue open sessions across the restart instead of orphaning them
    threading.Thread(target=ticker, daemon=True).start()
    if GEOCODE:
        # worker does its own startup backfill; never run it on the main thread (it must reach
        # client.connect() so the car keeps being logged).
        threading.Thread(target=geocode_worker, daemon=True).start()
    if st(VIN)["vstate"] is None:   # nothing open to resume -> seed asleep so there's one state row
        with lock:
            set_vstate(VIN, "asleep", now())
    global mqtt_client
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="tesla-tlwriter")
    mqtt_client = client   # publish_ha() uses this to push derived HA topics
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
