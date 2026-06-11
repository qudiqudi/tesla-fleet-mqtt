#!/usr/bin/env python3
"""
One-time: shift tlwriter's UTC-written rows into local time, after the switch to local-time storage.

This stack's DB is a copy of a teslalogger DB, whose schema stores wall-clock LOCAL time. tlwriter
originally wrote UTC, so until the local-time switch its rows are ~2h behind the imported history
and the (now local) Grafana datasource. This shifts ONLY those tlwriter rows into local time.

tlwriter rows are the ones past the "fork": the highest id this DB still shares with the source
teslalogger DB (the snapshot was a byte copy, so ids <= fork are identical in both; tlwriter's
later inserts diverge). Everything <= fork is the untouched local snapshot.

Offset is +2h: the tlwriter UTC era is summer (CEST). The dry run prints the date span so you can
confirm it doesn't cross a DST boundary (it doesn't). DRY RUN by default; set CONFIRM=yes to apply.
A marker row makes a second apply a no-op. Reads the source teslalogger DB (TL_DB_*) to find the
fork; writes this stack's DB (DB_*/TLW_DB_NAME). Run ONCE, after the local-time deploy:
  docker exec -e CONFIRM=yes tesla-tools python migration/fix-timezone.py
"""
import os

import pymysql

DST = dict(host=os.environ.get("DB_HOST", "mariadb"), port=int(os.environ.get("DB_PORT", "3306")),
           user=os.environ.get("DB_USER", "tesla"), password=os.environ["DB_PASSWORD"],
           database=os.environ.get("TLW_DB_NAME", "teslalogger"), connect_timeout=10, autocommit=True)
SRC = dict(host=os.environ.get("TL_DB_HOST", "teslalogger-db"), port=int(os.environ.get("TL_DB_PORT", "3306")),
           user=os.environ.get("TL_DB_USER", "teslalogger"), password=os.environ["TL_DB_PASSWORD"],
           database=os.environ.get("TL_DB_NAME", "teslalogger"), connect_timeout=10)
OFFSET_H = int(os.environ.get("TZ_SHIFT_HOURS", "2"))   # UTC -> CEST
APPLY = os.environ.get("CONFIRM", "").lower() in ("1", "yes", "true")
MARKER = "fix-timezone-v1"
# Safety floor: only shift rows from the tlwriter era (its first day onward). find_fork trusts the
# source teslalogger DB to still match the snapshot, but the live teslalogger modifies its own old
# rows (e.g. chargingstate cost/UnplugDate), so a fork can land far too low and try to drag in years
# of imported history. This floor makes that impossible -- nothing before the era is ever shifted.
ERA_START = os.environ.get("TLW_ERA_START", "2026-06-08 00:00:00")

# table -> (fork column present in both DBs, [datetime cols to shift], upper-bound id).
# The upper bound is the max id captured just before the local-time deploy: rows in (fork, cap]
# are the UTC rows tlwriter wrote; rows above cap are the local rows it writes after the deploy
# (and must NOT be shifted). Run promptly so few UTC rows are written between capture and deploy.
TABLES = {
    "pos":           ("Datum",     ["Datum"],                            941850),
    "drivestate":    ("StartDate", ["StartDate", "EndDate"],             3859),
    "chargingstate": ("StartDate", ["StartDate", "EndDate", "UnplugDate"], 1236),
    "charging":      ("Datum",     ["Datum"],                            215655),
    "state":         ("StartDate", ["StartDate", "EndDate"],             6214),
    "shiftstate":    ("StartDate", ["StartDate", "EndDate"],             36),
    "car_version":   ("StartDate", ["StartDate"],                        100),
}


def one(cur, sql, args=()):
    cur.execute(sql, args)
    r = cur.fetchone()
    return r[0] if r else None


def has_col(cur, table, col):
    return one(cur, "SELECT COUNT(*) FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE()"
               " AND TABLE_NAME=%s AND COLUMN_NAME=%s", (table, col)) > 0


def cest_safe(lo, hi):
    # the fixed +OFFSET_H is the CEST (+2h) offset; only correct when the whole shifted span is
    # unambiguously CEST (Apr-Sep). Refuse otherwise so a stray winter/CET row isn't shifted wrong.
    return bool(lo) and bool(hi) and 4 <= lo.month <= 9 and 4 <= hi.month <= 9


def find_fork(a, b, table, col):
    """Highest id with identical <col> in both DBs (the snapshot boundary). The copy is a byte
    snapshot, so ids form an identical prefix then diverge -> binary search the boundary."""
    with a.cursor() as ca, b.cursor() as cb:
        amax = one(ca, "SELECT MAX(id) FROM %s" % table) or 0
        bmax = one(cb, "SELECT MAX(id) FROM %s" % table) or 0
        hi = min(amax, bmax)
        if hi == 0:
            return 0
        lo, fork = 1, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            av = one(ca, "SELECT %s FROM %s WHERE id=%%s" % (col, table), (mid,))
            bv = one(cb, "SELECT %s FROM %s WHERE id=%%s" % (col, table), (mid,))
            if av is not None and av == bv:
                fork = mid; lo = mid + 1
            else:
                hi = mid - 1
        # sanity: the snapshot is a contiguous identical prefix, so a window just below the fork
        # must all match and the row just above must diverge. Catches a binary-search overshoot
        # onto a coincidental timestamp collision above the true fork.
        for probe in range(fork, max(0, fork - 3), -1):
            if one(ca, "SELECT %s FROM %s WHERE id=%%s" % (col, table), (probe,)) \
               != one(cb, "SELECT %s FROM %s WHERE id=%%s" % (col, table), (probe,)):
                raise SystemExit("fork detection unsafe for %s (id %d below fork %d diverges); aborting"
                                 % (table, probe, fork))
        nxt_a = one(ca, "SELECT %s FROM %s WHERE id=%%s" % (col, table), (fork + 1,))
        nxt_b = one(cb, "SELECT %s FROM %s WHERE id=%%s" % (col, table), (fork + 1,))
        if nxt_a is not None and nxt_a == nxt_b:
            raise SystemExit("fork detection unsafe for %s (id %d matches both DBs); aborting"
                             % (table, fork + 1))
        return fork


def main():
    a = pymysql.connect(**DST)
    with a.cursor() as c:
        c.execute("CREATE TABLE IF NOT EXISTS _tlw_migrations (name VARCHAR(64) PRIMARY KEY,"
                  " applied_at DATETIME)")
        if one(c, "SELECT COUNT(*) FROM _tlw_migrations WHERE name=%s", (MARKER,)):
            print("already applied (%s) -- nothing to do" % MARKER); return
    # source teslalogger DB -- only reached (for fork detection) when not yet applied, so an
    # already-applied re-run is a clean no-op even after teslalogger has been torn down
    b = pymysql.connect(**SRC)
    print("DRY RUN (set CONFIRM=yes to apply), shift = +%dh\n" % OFFSET_H if not APPLY
          else "APPLYING shift = +%dh\n" % OFFSET_H)

    total = 0
    for table, (forkcol, cols, cap) in TABLES.items():
        with a.cursor() as ca:
            cols = [col for col in cols if has_col(ca, table, col)]
            if not cols or not has_col(ca, table, forkcol):
                print("  %-13s: skipped (columns missing)" % table); continue
            fork = find_fork(a, b, table, forkcol)
            where = "id>%s AND id<=%s AND " + forkcol + ">=%s"   # era floor -- see ERA_START
            wargs = (fork, cap, ERA_START)
            n = one(ca, "SELECT COUNT(*) FROM %s WHERE %s" % (table, where), wargs)
            span = "—"
            if n:
                lo = one(ca, "SELECT MIN(%s) FROM %s WHERE %s" % (forkcol, table, where), wargs)
                hi = one(ca, "SELECT MAX(%s) FROM %s WHERE %s" % (forkcol, table, where), wargs)
                span = "%s .. %s" % (lo, hi)
            print("  %-13s fork id=%-7d shift (%d,%d]>=%s = %-5d %s"
                  % (table, fork, fork, cap, ERA_START[:10], n, span))
            total += n
            if APPLY and n:
                if not cest_safe(lo, hi):
                    raise SystemExit("  %s span %s is not wholly within Apr-Sep; the fixed +%dh (CEST)"
                                     " shift could be wrong across a DST boundary -- aborting before any"
                                     " write. Inspect and shift manually." % (table, span, OFFSET_H))
                sets = ", ".join("%s = %s + INTERVAL %d HOUR" % (c, c, OFFSET_H) for c in cols)
                ca.execute("UPDATE %s SET %s WHERE %s" % (table, sets, where), wargs)

    if APPLY:
        with a.cursor() as c:
            c.execute("INSERT INTO _tlw_migrations (name, applied_at) VALUES (%s, NOW())", (MARKER,))
        print("\napplied: shifted %d row(s) into local time" % total)
    else:
        print("\nwould shift %d row(s). Re-run with CONFIRM=yes to apply." % total)


if __name__ == "__main__":
    main()
