#!/usr/bin/env python3
"""
One-time backfill of charger names onto past charge stops, the historical counterpart to the
live naming tlwriter does on charge start. For every charge location (chargingstate.Pos) that
still carries a plain street address, look up the charging operator and rename it after them --
Open Charge Map first (if OCM_API_KEY is set, free key from openchargemap.org), then OSM's
amenity=charging_station via Overpass. Spots neither source knows keep their street address;
home charging (within HOME_RADIUS of HOME_LAT/HOME_LNG) is left as HOME_LABEL.

Each distinct location is looked up once (throttled) and applied to every charge there. Idempotent:
once a stop is named after its operator it no longer matches the street-address filter, so re-runs
only retry the spots still unnamed. Writes this stack's teslalogger-schema DB (DB_*/TLW_DB_NAME).
Run in the tools container:
  docker exec -e OCM_API_KEY=... tesla-tools python migration/name-chargers.py
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pymysql

DB = dict(host=os.environ.get("DB_HOST", "mariadb"), port=int(os.environ.get("DB_PORT", "3306")),
          user=os.environ.get("DB_USER", "tesla"), password=os.environ["DB_PASSWORD"],
          database=os.environ.get("TLW_DB_NAME", "teslalogger"), connect_timeout=10, autocommit=True)
VIN = os.environ.get("TESLA_VIN")

HOME_LABEL = os.environ.get("HOME_LABEL", "Home")
HOME_RADIUS = float(os.environ.get("HOME_RADIUS", "50"))
try:
    HOME = (float(os.environ["HOME_LAT"]), float(os.environ["HOME_LNG"]))
except (KeyError, ValueError):
    HOME = None

UA = os.environ.get("GEOCODE_USER_AGENT",
                    "tesla-fleet-mqtt/1.0 (https://github.com/qudiqudi/tesla-fleet-mqtt)")
MIN_INTERVAL = float(os.environ.get("GEOCODE_MIN_INTERVAL", "1.1"))
CHARGER_RADIUS = float(os.environ.get("CHARGER_RADIUS", "75"))
OCM_API_KEY = os.environ.get("OCM_API_KEY", "")
OCM_API_URL = os.environ.get("OCM_API_URL", "https://api.openchargemap.io/v3/poi/")
OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")

# a charge stop is a candidate for naming if it has no charger name yet: empty, or a street address
# (ours "<5-digit postcode> City, Road" or teslalogger's foreign "<cc>-..."). HOME_LABEL is excluded.
STREET = ("(p.address IS NULL OR p.address='' "
          "OR p.address REGEXP '^[0-9]{5} ' OR p.address REGEXP '^[a-z]{2}-')")


def _charger_label(operator, place):
    operator, place = (operator or "").strip(), (place or "").strip()
    if operator and place and place.lower() not in operator.lower():
        return ("%s, %s" % (operator, place))[:255]
    return (operator or place)[:255] or None


def _fetch_json(req, timeout):
    # OCM/Overpass rate-limit (429) and overload (5xx) in bursts; retry those + transient network
    # errors with a short backoff so a straggler isn't dropped just because the API was busy
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


def ocm_charger(lat, lng):
    if not OCM_API_KEY:
        return None
    params = urllib.parse.urlencode({"output": "json", "latitude": "%.6f" % lat, "longitude": "%.6f" % lng,
                                     "distance": CHARGER_RADIUS / 1000.0, "distanceunit": "KM",
                                     "maxresults": "1", "key": OCM_API_KEY})
    req = urllib.request.Request(OCM_API_URL + "?" + params, headers={"User-Agent": UA})
    d = _fetch_json(req, 15) or []
    if not d:
        return None
    poi = d[0]
    op = (poi.get("OperatorInfo") or {}).get("Title") or ""
    ai = poi.get("AddressInfo") or {}
    if op.lower() in ("", "(unknown operator)", "unknown"):
        op = ai.get("Title") or ""
    return _charger_label(op, ai.get("AddressLine1") or ai.get("Town"))


def osm_charger(lat, lng):
    q = ("[out:json][timeout:25];nwr(around:%d,%.6f,%.6f)[amenity=charging_station];out tags center 1;"
         % (int(CHARGER_RADIUS), lat, lng))
    req = urllib.request.Request(OVERPASS_URL, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": UA})
    els = (_fetch_json(req, 30) or {}).get("elements", [])
    if not els:
        return None
    t = els[0].get("tags", {})
    op = t.get("operator") or t.get("brand") or t.get("network") or t.get("name")
    return _charger_label(op, t.get("addr:street")) if op else None


def charger_name(lat, lng):
    for fn in (ocm_charger, osm_charger):
        try:
            name = fn(lat, lng)
            if name:
                return name
        except Exception as e:
            print("  charger lookup (%s) failed: %s" % (fn.__name__, e))
    return None


def main():
    db = pymysql.connect(**DB)
    car, args = "", []
    if VIN:
        with db.cursor() as c:
            c.execute("SELECT id FROM cars WHERE vin=%s", (VIN,))
            row = c.fetchone()
        if row:
            car, args = " AND cs.CarID=%s", [row[0]]
            print("scoping to CarID=%s (VIN %s)" % (row[0], VIN))

    home = " AND ST_Distance_Sphere(POINT(p.lng,p.lat),POINT(%s,%s))>%s" if HOME else ""
    hargs = [HOME[1], HOME[0], HOME_RADIUS] if HOME else []
    with db.cursor() as c:
        c.execute("SELECT DISTINCT p.id, p.lat, p.lng FROM chargingstate cs JOIN pos p ON p.id=cs.Pos"
                  " WHERE p.lat IS NOT NULL AND NOT (p.lat=0 AND p.lng=0) AND " + STREET + car + home,
                  args + hargs)
        rows = c.fetchall()
    groups = {}
    for pid, lat, lng in rows:
        groups.setdefault((round(float(lat), 4), round(float(lng), 4)), []).append((pid, float(lat), float(lng)))
    print("naming: %d charge stop(s) across %d distinct location(s)" % (len(rows), len(groups)))

    last, named, hit = 0.0, 0, 0
    for i, (_, members) in enumerate(sorted(groups.items()), 1):
        _, lat, lng = members[0]
        ids = [pid for pid, _, _ in members]
        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        name = charger_name(lat, lng)
        if not name:
            print("  [%d/%d] %.5f,%.5f -- no charger found, left as-is" % (i, len(groups), lat, lng))
            continue
        ph = ",".join(["%s"] * len(ids))
        with db.cursor() as c:
            # re-check STREET so a re-run can't clobber a stop that's since been named
            c.execute("UPDATE pos p SET p.address=%s WHERE p.id IN (" + ph + ") AND " + STREET,
                      [name] + ids)
        hit += 1
        named += len(ids)
        print("  [%d/%d] %s (%d stop(s))" % (i, len(groups), name, len(ids)))
    print("named %d charge stop(s) at %d location(s)" % (named, hit))


if __name__ == "__main__":
    main()
