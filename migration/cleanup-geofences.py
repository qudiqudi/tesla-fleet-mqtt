#!/usr/bin/env python3
"""
One-time cleanup of teslalogger geofence labels left in pos.address, for sunsetting teslalogger.

teslalogger wrote a geofence's name into pos.address for every position inside it (home, work,
named chargers). With teslalogger gone those labels are frozen text and new visits aren't named,
so this normalises them to what tlwriter produces live:

  - Home: every position within HOME_RADIUS m of HOME_LAT/HOME_LNG is set to HOME_LABEL -- this
    stack's own home geofence (the same one tlwriter applies). No geocoding.
  - Everything else that isn't already a reverse-geocoded street address (work zones, charger
    names -- anything not matching our "<postcode> City, Road" or teslalogger's "<cc>-..." forms)
    is reverse-geocoded to a street address via OSM Nominatim. Each DISTINCT location is geocoded
    once (throttled to Nominatim's 1 req/s policy) and applied to all rows there, so thousands of
    rows cost only as many requests as there are places.

Writes this stack's teslalogger-schema DB (the one tlwriter writes: DB_HOST/DB_USER/DB_PASSWORD,
TLW_DB_NAME). Idempotent: street-addressed rows and HOME_LABEL rows are skipped on re-run. Reuses
tlwriter's HOME_*/NOMINATIM_* env so the home zone matches. Run in the tools container:
  docker exec tesla-tools python migration/cleanup-geofences.py
"""
import json
import os
import time
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

NOMINATIM_URL = os.environ.get("NOMINATIM_URL", "https://nominatim.openstreetmap.org/reverse")
UA = os.environ.get("GEOCODE_USER_AGENT",
                    "tesla-fleet-mqtt/1.0 (https://github.com/qudiqudi/tesla-fleet-mqtt)")
MIN_INTERVAL = float(os.environ.get("GEOCODE_MIN_INTERVAL", "1.1"))

# A row still carries a teslalogger geofence label if its address is non-empty and is NOT one of
# the two reverse-geocoded street-address shapes: ours ("<5-digit postcode> City, Road N") or
# teslalogger's foreign form ("<cc>-<postcode> City, Road"). HOME_LABEL is already ours. Excludes
# 0,0 no-fix glitches. Same WHERE for the SELECT and the idempotency guard on UPDATE.
LABEL_FILTER = ("address IS NOT NULL AND address<>'' AND address<>%s "
                "AND address NOT REGEXP '^[0-9]{5} ' AND address NOT REGEXP '^[a-z]{2}-' "
                "AND lat IS NOT NULL AND NOT (lat=0 AND lng=0)")


def geocode_address(lat, lng):
    # same shape as tlwriter.geocode_address; never returns empty
    params = urllib.parse.urlencode({"lat": "%.6f" % lat, "lon": "%.6f" % lng,
                                     "format": "jsonv2", "zoom": "18", "addressdetails": "1"})
    req = urllib.request.Request(NOMINATIM_URL + "?" + params, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode("utf-8", "replace")) or {}
    a = d.get("address", {})
    city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county") or ""
    left = ("%s %s" % (a.get("postcode", ""), city)).strip()
    right = ("%s %s" % (a.get("road", ""), a.get("house_number", ""))).strip()
    addr = ", ".join(p for p in (left, right) if p)
    addr = addr or d.get("display_name") or "%.5f, %.5f" % (lat, lng)
    return addr[:255]


def main():
    db = pymysql.connect(**DB)
    car, args = "", []
    if VIN:
        with db.cursor() as c:
            c.execute("SELECT id FROM cars WHERE vin=%s", (VIN,))
            row = c.fetchone()
        if row:
            car, args = " AND CarID=%s", [row[0]]
            print("scoping to CarID=%s (VIN %s)" % (row[0], VIN))

    # 1. Home -> HOME_LABEL (no geocoding). Names previously-unaddressed home rows too.
    if HOME:
        with db.cursor() as c:
            n = c.execute(
                "UPDATE pos SET address=%s WHERE lat IS NOT NULL"
                " AND ST_Distance_Sphere(POINT(lng,lat),POINT(%s,%s))<=%s"
                " AND COALESCE(address,'')<>%s" + car,
                [HOME_LABEL, HOME[1], HOME[0], HOME_RADIUS, HOME_LABEL] + args)
        print("home: relabelled %d position(s) -> %r" % (n, HOME_LABEL))
    else:
        print("home: HOME_LAT/HOME_LNG unset -- skipping (home rows will be geocoded as addresses)")

    # 2. Everything else still carrying a geofence label -> reverse-geocoded street address,
    #    one Nominatim request per distinct ~10 m location.
    where = LABEL_FILTER + car
    with db.cursor() as c:
        c.execute("SELECT id, lat, lng FROM pos WHERE " + where, [HOME_LABEL] + args)
        rows = c.fetchall()
    groups = {}
    for pid, lat, lng in rows:
        groups.setdefault((round(float(lat), 4), round(float(lng), 4)), []).append((pid, float(lat), float(lng)))
    print("geocoding: %d label rows across %d distinct locations" % (len(rows), len(groups)))

    last, done = 0.0, 0
    for i, (_, members) in enumerate(sorted(groups.items()), 1):
        _, lat, lng = members[0]
        ids = [pid for pid, _, _ in members]
        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        try:
            addr = geocode_address(lat, lng)
        except Exception as e:
            print("  [%d/%d] %.5f,%.5f FAILED: %s" % (i, len(groups), lat, lng, e))
            continue
        ph = ",".join(["%s"] * len(ids))
        with db.cursor() as c:
            # re-check LABEL_FILTER in the UPDATE so a concurrent/prior run can't clobber a row
            # that's since been street-addressed (keeps re-runs safe)
            c.execute("UPDATE pos SET address=%s WHERE id IN (" + ph + ") AND " + LABEL_FILTER,
                      [addr] + ids + [HOME_LABEL])
        done += len(ids)
        print("  [%d/%d] %s (%d row(s))" % (i, len(groups), addr, len(ids)))
    print("geocoded %d row(s) across %d locations" % (done, len(groups)))


if __name__ == "__main__":
    main()
