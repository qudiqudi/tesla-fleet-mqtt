#!/usr/bin/env python3
"""
Set the MySQL datasource's session time zone to the DB's local zone (SYSTEM).

The teslalogger schema stores wall-clock LOCAL time, and this DB is a copy of a teslalogger DB,
so tlwriter writes local time too (TLW_TZ). Grafana's `$__time` / `$__timeFilter` use
UNIX_TIMESTAMP(), which interprets a DATETIME in the *session* time zone — so the session tz must
match the stored zone, otherwise every time-based panel renders off (the imported history showed
~2h late). The MariaDB container runs TZ=Europe/Berlin, so its SYSTEM zone IS the local zone and
follows DST; SET time_zone='SYSTEM' makes Grafana read the local columns as local. Idempotent.

Uses "SYSTEM", NOT a named zone like "Europe/Berlin": MariaDB only accepts named zones once its
time-zone tables are loaded, which they aren't here (CONVERT_TZ on a named zone returns NULL) —
Grafana would then error on every query. "SYSTEM" needs no tz tables and is DST-aware via the OS.
(If a deployment's MariaDB system zone isn't local, set DST_SESSION_TZ to a fixed offset like
"+02:00" instead — but that won't follow DST.)

The equivalent one-field UI change: Connections -> Data sources -> the MySQL datasource ->
"Session timezone" = SYSTEM -> Save & test.

Run in the tools container (admin/editor token; this stack's Grafana listens on :3003):
  docker exec -e DST_GRAFANA_TOKEN=... -e DST_GRAFANA=http://grafana:3003 \
    tesla-tools python migration/grafana-fix-datasource-tz.py
"""
import os

from _grafana import api

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
TZ = os.environ.get("DST_SESSION_TZ", "SYSTEM")   # DB stores local; SYSTEM = container's Europe/Berlin


def main():
    dss = api("GET", "/api/datasources", TOK, DST).json()
    mysql = [d for d in dss if d.get("type") == "mysql"]
    if not mysql:
        print("no mysql datasource found")
        return
    for d in mysql:
        full = api("GET", "/api/datasources/uid/%s" % d["uid"], TOK, DST).json()
        jd = full.setdefault("jsonData", {})
        if jd.get("timezone") == TZ:
            print("  %s: already %s" % (full["name"], TZ))
            continue
        jd["timezone"] = TZ
        # PUT keeps the existing password (secureJsonData is only changed if sent).
        r = api("PUT", "/api/datasources/uid/%s" % d["uid"], TOK, DST, payload=full)
        print("  %s: session timezone -> %s (%s)" % (full["name"], TZ, r.status_code))


if __name__ == "__main__":
    main()
