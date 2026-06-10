#!/usr/bin/env python3
"""
Fix teslalogger SQL that breaks on current MariaDB/Grafana. These queries also fail on
teslalogger's own (watchtower-updated) Grafana -- they are query bugs, not migration
regressions. Applies four idempotent rewrites to every panel target in the folder:

  1. `ORDER BY time_sec[ ASC|DESC]`  -> `ORDER BY 1`
     teslalogger selects `$__time(col)` (aliased "time") as the first column but orders by
     `time_sec`, which only exists in later UNION branches -> "Unknown column 'time_sec'".
     The time column is always first, so ordering by position 1 is equivalent.

  2. `avg(lat)` / `avg(lng)`  -> `avg(lat) as lat` / `avg(lng) as lng`
     The Visited map aggregates coordinates but doesn't alias them, so the map wrapper's
     `SELECT lat, lng` can't find the columns.

  3. `$__timeGroup(col, 5m)`  -> manual `UNIX_TIMESTAMP(col) DIV <secs> * <secs>`
     This Grafana's MySQL `$__timeGroup` macro errors here; also rewrite the matching
     `$__time(col)` in SELECT and the `ORDER BY col` to position 1 so GROUP BY stays valid.

Idempotent. Run in the tools container (this stack listens on :3003):
  docker exec -e DST_GRAFANA_TOKEN=... -e DST_GRAFANA=http://grafana:3003 \
    tesla-tools python migration/grafana-fix-sql.py
"""
import os
import re

from _grafana import folder_uid, for_each_dashboard, search_dashboards, walk_panels

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def fix_sql(sql):
    orig = sql
    # 1. ORDER BY time_sec -> ORDER BY 1 (standalone only; leaves "ORDER BY x, time_sec")
    sql = re.sub(r'(?i)order\s+by\s+time_sec(\s+(?:asc|desc))?',
                 lambda m: 'ORDER BY 1' + (m.group(1) or ''), sql)
    # 2. alias aggregated coordinates so the map wrapper can select lat/lng
    sql = sql.replace('avg(lat),', 'avg(lat) as lat,').replace('avg(lng),', 'avg(lng) as lng,')
    # 3. $__timeGroup(col, Nunit) -> manual bucket; keep SELECT/GROUP BY/ORDER BY consistent
    m = re.search(r'\$__timeGroup\((\w+(?:\.\w+)?)\s*,\s*(\d+)([smhd])\)', sql)
    if m:
        col, n, unit = m.group(1), int(m.group(2)), m.group(3)
        secs = n * UNIT[unit]
        bucket = "UNIX_TIMESTAMP(%s) DIV %d * %d" % (col, secs, secs)
        sql = re.sub(r'\$__timeGroup\([^)]*\)', '1', sql)            # GROUP BY ... -> GROUP BY 1
        sql = re.sub(r'\$__time\(%s\)' % re.escape(col), bucket + ' AS time', sql)
        sql = re.sub(r'(?i)order\s+by\s+%s(\s+(?:asc|desc))?' % re.escape(col),
                     lambda mm: 'ORDER BY 1' + (mm.group(1) or ''), sql)

    # 4. The Trip dashboards filter by `Start_address like '%$Textfilter%' or End_address like ...`.
    #    tlwriter doesn't reverse-geocode, so those columns are NULL on its drives, and
    #    `NULL LIKE '%%'` is NULL (not true) -> every un-geocoded trip is silently hidden, even
    #    with an empty filter. COALESCE to '' so an empty filter still matches NULL-address rows.
    sql = re.sub(r"(?i)\b(Start_address|End_address)\s+like\b",
                 lambda m: "COALESCE(%s,'') like" % m.group(1), sql)
    return sql if sql != orig else None


def fix_panel(p):
    n = 0
    for tgt in p.get("targets", []):
        sql = tgt.get("rawSql")
        if not sql:
            continue
        new = fix_sql(sql)
        if new:
            tgt["rawSql"] = new
            n += 1
    return n


def main():
    fu = folder_uid(FOLDER, TOK, DST)
    if not fu:
        print("folder '%s' not found" % FOLDER); return
    items = search_dashboards(fu, TOK, DST)
    print("%d dashboards in '%s'" % (len(items), FOLDER))
    for_each_dashboard(fu, lambda d: walk_panels(d.get("panels", []), fix_panel),
                       TOK, DST, "  %s: fixed %d query(ies) -> %s", items=items)


if __name__ == "__main__":
    main()
