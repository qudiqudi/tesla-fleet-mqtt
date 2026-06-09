#!/usr/bin/env python3
"""
One-time correction for rows tlwriter stored while the car's display unit was miles (before
the SettingDistanceUnit normalisation landed). Multiplies the distance/speed columns by
1.609344 for rows since the switch, converting miles/(mph) back to km/(km-h).

Scoped by time (UNIT_CUTOFF), because old rows legitimately have small odometer values from
the car's early life. Idempotent: gated on miles pos rows still existing since the cutoff
(once corrected, pos.odometer > 40000, so a re-run is a no-op and won't double-convert the
drivestate/charging rows either).

  docker exec tesla-tools python /app/migration/fix-units.py             # dry-run (counts)
  docker exec -e APPLY=1 tesla-tools python /app/migration/fix-units.py   # apply
"""
import os
import sys
import pymysql

MI = 1.609344
CUTOFF = os.environ.get("UNIT_CUTOFF", "2026-06-08 11:47:00")
VIN = os.environ.get("TESLA_VIN")
APPLY = os.environ.get("APPLY") == "1"

cn = pymysql.connect(host=os.environ.get("DB_HOST", "mariadb"),
                     port=int(os.environ.get("DB_PORT", "3306")),
                     user=os.environ.get("DB_USER", "tesla"),
                     password=os.environ["DB_PASSWORD"],
                     database=os.environ.get("TLW_DB_NAME", "teslalogger"),
                     autocommit=False)


def scalar(cur, sql, args):
    cur.execute(sql, args)
    return cur.fetchone()[0]


def main():
    with cn.cursor() as cur:
        if VIN:
            cur.execute("SELECT id FROM cars WHERE vin=%s", (VIN,))
            row = cur.fetchone()
            car = row[0] if row else 1
        else:
            car = 1
        # idempotency gate: miles pos rows still present since the cutoff?
        miles_pos = scalar(cur, "SELECT COUNT(*) FROM pos WHERE CarID=%s AND Datum>=%s "
                                "AND odometer>0 AND odometer<40000", (car, CUTOFF))
        if miles_pos == 0:
            print("no miles rows since %s — already corrected, nothing to do." % CUTOFF)
            return
        ds = scalar(cur, "SELECT COUNT(*) FROM drivestate WHERE CarID=%s AND StartDate>=%s", (car, CUTOFF))
        ch = scalar(cur, "SELECT COUNT(*) FROM charging WHERE CarID=%s AND Datum>=%s "
                         "AND ideal_battery_range_km>0", (car, CUTOFF))
        print("car=%s cutoff=%s  pos=%d  drivestate=%d  charging=%d  (apply=%s)"
              % (car, CUTOFF, miles_pos, ds, ch, APPLY))
        if not APPLY:
            print("dry-run only. set APPLY=1 to apply.")
            return
        # pos: odometer<40000 guard keeps it idempotent and avoids touching real km rows
        cur.execute("""UPDATE pos SET odometer=odometer*%s, speed=ROUND(speed*%s),
                       ideal_battery_range_km=ideal_battery_range_km*%s,
                       battery_range_km=battery_range_km*%s
                       WHERE CarID=%s AND Datum>=%s AND odometer>0 AND odometer<40000""",
                    (MI, MI, MI, MI, car, CUTOFF))
        p = cur.rowcount
        cur.execute("""UPDATE drivestate SET speed_max=ROUND(speed_max*%s)
                       WHERE CarID=%s AND StartDate>=%s AND speed_max>0""", (MI, car, CUTOFF))
        d = cur.rowcount
        cur.execute("""UPDATE charging SET ideal_battery_range_km=ideal_battery_range_km*%s,
                       battery_range_km=battery_range_km*%s
                       WHERE CarID=%s AND Datum>=%s AND ideal_battery_range_km>0""",
                    (MI, MI, car, CUTOFF))
        c = cur.rowcount
    cn.commit()
    print("applied: pos=%d drivestate=%d charging=%d rows corrected." % (p, d, c))


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        print("missing env: %s" % e); sys.exit(1)
