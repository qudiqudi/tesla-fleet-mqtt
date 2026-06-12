#!/usr/bin/env python3
"""
Clear "0.00000, 0.00000" no-fix addresses from pos.address. A GPS glitch (lat=0, lng=0) used to be
reverse-geocoded to a literal coordinate string by tlwriter's never-empty fallback; the writer now
skips 0,0, but old rows keep the label. This nulls them, so a glitch trip endpoint renders blank
instead of "0.00000, 0.00000".

DRY RUN by default; set CONFIRM=yes to apply. Writes this stack's teslalogger-schema DB
(DB_*/TLW_DB_NAME). Run in the tools container:
  docker exec -e CONFIRM=yes tesla-tools python migration/clear-nofix-addresses.py
"""
import os

import pymysql

DB = dict(host=os.environ.get("DB_HOST", "mariadb"), port=int(os.environ.get("DB_PORT", "3306")),
          user=os.environ.get("DB_USER", "tesla"), password=os.environ["DB_PASSWORD"],
          database=os.environ.get("TLW_DB_NAME", "teslalogger"), connect_timeout=10, autocommit=True)
APPLY = os.environ.get("CONFIRM", "").lower() in ("1", "yes", "true")
WHERE = "lat=0 AND lng=0 AND address IS NOT NULL AND address<>''"


def main():
    db = pymysql.connect(**DB)
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) FROM pos WHERE " + WHERE)
        n = c.fetchone()[0]
    print("%d no-fix (0,0) address(es) to clear" % n)
    if APPLY and n:
        with db.cursor() as c:
            c.execute("UPDATE pos SET address=NULL WHERE " + WHERE)
        print("cleared %d" % n)
    elif not APPLY:
        print("dry run -- set CONFIRM=yes to apply")


if __name__ == "__main__":
    main()
