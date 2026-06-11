#!/usr/bin/env python3
"""
Collapse duplicate car_version rows. teslalogger's Firmware panel wants one row per firmware change;
before the restart-dedup fix, tlwriter re-inserted the unchanged firmware on every boot, so a deploy
storm left runs of identical rows. This keeps the first row of each consecutive same-version run (the
real change time) and deletes the rest -- a legitimately recurring version (e.g. a downgrade) keeps a
fresh row because the run is broken by the different version in between.

Per car. DRY RUN by default; set CONFIRM=yes to delete. Writes this stack's teslalogger-schema DB
(DB_*/TLW_DB_NAME). Run in the tools container:
  docker exec -e CONFIRM=yes tesla-tools python migration/dedup-car-version.py
"""
import os

import pymysql

DB = dict(host=os.environ.get("DB_HOST", "mariadb"), port=int(os.environ.get("DB_PORT", "3306")),
          user=os.environ.get("DB_USER", "tesla"), password=os.environ["DB_PASSWORD"],
          database=os.environ.get("TLW_DB_NAME", "teslalogger"), connect_timeout=10, autocommit=True)
APPLY = os.environ.get("CONFIRM", "").lower() in ("1", "yes", "true")


def main():
    db = pymysql.connect(**DB)
    with db.cursor() as c:
        c.execute("SELECT DISTINCT CarID FROM car_version ORDER BY CarID")
        cars = [r[0] for r in c.fetchall()]
    print("DRY RUN (set CONFIRM=yes to delete)\n" if not APPLY else "DELETING duplicates\n")

    total = 0
    for car in cars:
        with db.cursor() as c:
            c.execute("SELECT id, version FROM car_version WHERE CarID=%s ORDER BY id", (car,))
            rows = c.fetchall()
        prev, dead = None, []
        for cid, version in rows:
            if version == prev:
                dead.append(cid)
            else:
                prev = version
        print("  CarID %s: %d row(s), %d duplicate(s) -> %d after" % (car, len(rows), len(dead), len(rows) - len(dead)))
        total += len(dead)
        if APPLY and dead:
            ph = ",".join(["%s"] * len(dead))
            with db.cursor() as c:
                c.execute("DELETE FROM car_version WHERE id IN (" + ph + ")", dead)

    if APPLY:
        print("\ndeleted %d duplicate row(s)" % total)
    else:
        print("\nwould delete %d row(s). Re-run with CONFIRM=yes to apply." % total)


if __name__ == "__main__":
    main()
