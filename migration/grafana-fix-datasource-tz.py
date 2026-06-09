#!/usr/bin/env python3
"""
Set the MySQL datasource's session time zone to UTC.

tlwriter writes the DB in UTC wall-clock (timezone.utc), but the MariaDB container's SYSTEM
time zone is local (e.g. CEST). Grafana's `$__time` / `$__timeFilter` use UNIX_TIMESTAMP(),
which interprets a DATETIME in the *session* time zone — so without a session tz set, the UTC
values are read as local and every time-based panel renders ~2h off (the Status timeline shows
online/asleep at the wrong time, etc.). Setting the datasource session tz to UTC makes Grafana
read the UTC columns as UTC. Idempotent.

Uses the numeric offset "+00:00", NOT the named zone "UTC": MariaDB only accepts named zones
(SET time_zone='UTC') when its time-zone tables are loaded, which they usually aren't — Grafana
then errors on every query ("db query error: query failed"). "+00:00" always works.

The equivalent one-field UI change: Connections -> Data sources -> the MySQL datasource ->
"Session timezone" = +00:00 -> Save & test.

Run in the tools container (admin/editor token; this stack's Grafana listens on :3003):
  docker exec -e DST_GRAFANA_TOKEN=... -e DST_GRAFANA=http://grafana:3003 \
    tesla-tools python migration/grafana-fix-datasource-tz.py
"""
import os

import requests

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
TZ = os.environ.get("DST_SESSION_TZ", "+00:00")   # numeric offset; "UTC" needs MariaDB tz tables
h = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"}


def main():
    dss = requests.get("%s/api/datasources" % DST, headers=h, timeout=30).json()
    mysql = [d for d in dss if d.get("type") == "mysql"]
    if not mysql:
        print("no mysql datasource found")
        return
    for d in mysql:
        full = requests.get("%s/api/datasources/uid/%s" % (DST, d["uid"]), headers=h, timeout=30).json()
        jd = full.setdefault("jsonData", {})
        if jd.get("timezone") == TZ:
            print("  %s: already %s" % (full["name"], TZ))
            continue
        jd["timezone"] = TZ
        # PUT keeps the existing password (secureJsonData is only changed if sent).
        r = requests.put("%s/api/datasources/uid/%s" % (DST, d["uid"]), headers=h, timeout=30, json=full)
        print("  %s: session timezone -> %s (%s)" % (full["name"], TZ, r.status_code))


if __name__ == "__main__":
    main()
