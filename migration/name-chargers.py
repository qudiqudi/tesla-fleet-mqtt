#!/usr/bin/env python3
"""
One-time backfill of charger names onto past charge stops, the historical counterpart to the
live naming tlwriter does at charge end. For every charge location (chargingstate.Pos) still on a
plain street address, look up the charging operator and rename it -- Open Charge Map first (if
OCM_API_KEY is set, free key from openchargemap.org), then OSM's amenity=charging_station via
Overpass. Spots neither source knows keep their street address; home charging (within HOME_RADIUS
of HOME_LAT/HOME_LNG) is left as HOME_LABEL.

Co-located chargers (a Supercharger next to an Ionity) are disambiguated with the charge's recorded
fast_charger_brand: "Tesla" -> the Tesla operator, anything else (a third-party CCS) -> the non-Tesla
operator. Charges with no recorded brand (the tlwriter era, before it captured it) inherit the brand
of the other charges at the same location, so a spot you only ever Supercharged at resolves to Tesla.

Each distinct location is looked up once (throttled, with backoff on rate limits) and applied per
brand. Idempotent: once a stop is named after its operator it no longer matches the street-address
filter, so re-runs only retry the unnamed. Writes this stack's teslalogger-schema DB (DB_*/TLW_DB_NAME).
Run in the tools container:
  docker exec -e OCM_API_KEY=... tesla-tools python migration/name-chargers.py
"""
import json
import os
import re
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

# a charge stop still needs a name if it has none yet: empty, or a street address -- ours
# ("<5-digit postcode> City, Road") or teslalogger's foreign form ("<cc>-..."). HOME_LABEL never matches.
STREET_RE = re.compile(r"^(?:[0-9]{5} |[a-z]{2}-)", re.I)


def is_street(addr):
    return not (addr or "").strip() or bool(STREET_RE.match(addr))


def brand_class(b):
    # the charge's recorded fast_charger_brand -> "tesla" (Supercharger) / "third" (CCS) / None
    b = (b or "").strip().lower()
    if not b:
        return None
    return "tesla" if "tesla" in b else "third"


def needs_attention(address, eff):
    # work to do if the stop is still a street address, or its name's operator disagrees with the
    # brand the car reported (so a Supercharger mislabelled "Ionity" gets corrected, not just blanks)
    if is_street(address):
        return True
    name_tesla = "tesla" in (address or "").lower()
    return (eff == "tesla" and not name_tesla) or (eff == "third" and name_tesla)


def _charger_label(operator, place):
    operator, place = (operator or "").strip(), (place or "").strip()
    if operator and place and place.lower() not in operator.lower():
        return ("%s, %s" % (operator, place))[:255]
    return (operator or place)[:255] or None


def _select(cands, klass):
    # cands: nearest-first (operator, place); klass: "tesla"/"third"/None -> pick the matching operator
    cands = [(o, p) for o, p in cands if o]
    if not cands:
        return None
    if klass == "tesla":
        for o, p in cands:
            if "tesla" in o.lower():
                return _charger_label(o, p)
        return _charger_label("Tesla Supercharger", cands[0][1])   # SuC not mapped here, but brand says so
    if klass == "third":
        for o, p in cands:
            if "tesla" not in o.lower():
                return _charger_label(o, p)
    return _charger_label(*cands[0])                   # unknown brand, or only Tesla mapped -> nearest


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


def ocm_candidates(lat, lng):
    if not OCM_API_KEY:
        return []
    params = urllib.parse.urlencode({"output": "json", "latitude": "%.6f" % lat, "longitude": "%.6f" % lng,
                                     "distance": CHARGER_RADIUS / 1000.0, "distanceunit": "KM",
                                     "maxresults": "5", "key": OCM_API_KEY})
    req = urllib.request.Request(OCM_API_URL + "?" + params, headers={"User-Agent": UA})
    out = []
    for poi in _fetch_json(req, 15) or []:
        op = (poi.get("OperatorInfo") or {}).get("Title") or ""
        ai = poi.get("AddressInfo") or {}
        if op.lower() in ("", "(unknown operator)", "unknown"):
            op = ai.get("Title") or ""
        out.append((op, ai.get("AddressLine1") or ai.get("Town")))
    return out


def osm_candidates(lat, lng):
    q = ("[out:json][timeout:25];nwr(around:%d,%.6f,%.6f)[amenity=charging_station];out tags center;"
         % (int(CHARGER_RADIUS), lat, lng))
    req = urllib.request.Request(OVERPASS_URL, data=urllib.parse.urlencode({"data": q}).encode(),
                                 headers={"User-Agent": UA})
    out = []
    for e in (_fetch_json(req, 30) or {}).get("elements", []):
        t = e.get("tags", {})
        op = t.get("operator") or t.get("brand") or t.get("network") or t.get("name")
        if op:
            out.append((op, t.get("addr:street")))
    return out


def candidates(lat, lng):
    for fn in (ocm_candidates, osm_candidates):
        try:
            c = fn(lat, lng)
            if c:
                return c
        except Exception as e:
            print("  lookup (%s) failed: %s" % (fn.__name__, e))
    return []


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
    # ALL charge stops (named or not) so a location's recorded brands can name its brand-less stops
    with db.cursor() as c:
        c.execute("SELECT p.id, p.lat, p.lng, cs.fast_charger_brand, p.address"
                  " FROM chargingstate cs JOIN pos p ON p.id=cs.Pos"
                  " WHERE p.lat IS NOT NULL AND NOT (p.lat=0 AND p.lng=0)" + car + home, args + hargs)
        rows = c.fetchall()
    # group by location; resolve each stop's effective brand (own, else the location's recorded one)
    groups = {}
    for pid, lat, lng, brand, address in rows:
        groups.setdefault((round(float(lat), 4), round(float(lng), 4)), []).append(
            (pid, float(lat), float(lng), brand, address))
    todo = {}
    for k, members in groups.items():
        present = {brand_class(x[3]) for x in members} - {None}
        loc = "tesla" if present == {"tesla"} else "third" if present == {"third"} else None
        items = [(pid, address, brand_class(brand) or loc) for pid, _, _, brand, address in members]
        if any(needs_attention(a, e) for _, a, e in items):
            todo[k] = (members[0][1], members[0][2], items)
    print("naming: %d stop(s) to (re)name across %d location(s) (of %d charged locations)"
          % (sum(sum(needs_attention(a, e) for _, a, e in it) for _, _, it in todo.values()),
             len(todo), len(groups)))

    last, named = 0.0, 0
    for i, (_, (lat, lng, items)) in enumerate(sorted(todo.items()), 1):
        wait = MIN_INTERVAL - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        last = time.monotonic()
        cands = candidates(lat, lng)
        if not cands:
            print("  [%d/%d] %.5f,%.5f -- no charger found, left as-is" % (i, len(todo), lat, lng))
            continue
        by = {}   # effective brand -> stops needing attention
        for pid, addr, eff in items:
            if needs_attention(addr, eff):
                by.setdefault(eff, []).append((pid, addr))
        for klass, lst in by.items():
            name = _select(cands, klass)
            pids = [pid for pid, addr in lst if name and addr != name]   # skip no-ops -> idempotent
            if not pids:
                continue
            ph = ",".join(["%s"] * len(pids))
            with db.cursor() as c:
                c.execute("UPDATE pos SET address=%s WHERE id IN (" + ph + ")", [name] + pids)
            named += len(pids)
            tag = {"tesla": " [Tesla]", "third": " [CCS]"}.get(klass, "")
            print("  [%d/%d] %s (%d stop(s))%s" % (i, len(todo), name, len(pids), tag))
    print("(re)named %d charge stop(s)" % named)


if __name__ == "__main__":
    main()
