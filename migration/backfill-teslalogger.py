#!/usr/bin/env python3
"""
One-time backfill: copy teslalogger's historical drives and charges into our
MariaDB `tesla` DB (drives, charges) with original timestamps.

Reads teslalogger-db (TL_DB_*), writes our DB (DB_*). Idempotent (unique vin+start_ts,
source='backfill'). Run in the tools container:
  docker exec tesla-tools python migration/backfill-teslalogger.py
"""
import os
import sys
import pymysql

TL = dict(host=os.environ.get("TL_DB_HOST", "teslalogger-db"),
          port=int(os.environ.get("TL_DB_PORT", "3306")),
          user=os.environ.get("TL_DB_USER", "teslalogger"),
          password=os.environ["TL_DB_PASSWORD"],
          database=os.environ.get("TL_DB_NAME", "teslalogger"),
          connect_timeout=10, read_timeout=120)
DST = dict(host=os.environ.get("DB_HOST", "mariadb"),
           port=int(os.environ.get("DB_PORT", "3306")),
           user=os.environ.get("DB_USER", "tesla"),
           password=os.environ["DB_PASSWORD"],
           database=os.environ.get("DB_NAME", "tesla"),
           connect_timeout=10)
VIN = os.environ["TESLA_VIN"]


def main():
    src = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **TL)
    dst = pymysql.connect(autocommit=True, **DST)

    # map this VIN -> teslalogger CarID
    with src.cursor() as c:
        c.execute("SELECT id FROM cars WHERE vin=%s", (VIN,))
        row = c.fetchone()
        if not row:
            print("VIN %s not found in teslalogger cars table" % VIN); sys.exit(1)
        car_id = row["id"]
    print("teslalogger CarID=%s for VIN %s" % (car_id, VIN))

    # ---- drives ---- (simple PK joins only; max_speed left to the live sessionizer)
    print("fetching drives from teslalogger...", flush=True)
    with src.cursor() as c:
        c.execute("""
          SELECT ds.StartDate AS start_ts, ds.EndDate AS end_ts,
                 sp.odometer AS s_odo, ep.odometer AS e_odo,
                 sp.battery_level AS s_soc, ep.battery_level AS e_soc,
                 sp.lat AS s_lat, sp.lng AS s_lng, ep.lat AS e_lat, ep.lng AS e_lng,
                 sp.outside_temp AS otemp
          FROM drivestate ds
          JOIN pos sp ON sp.id = ds.StartPos
          JOIN pos ep ON ep.id = ds.EndPos
          WHERE ds.CarID=%s AND ds.EndDate IS NOT NULL
        """, (car_id,))
        drives = c.fetchall()
    print("  got %d drives, inserting..." % len(drives), flush=True)
    n = 0
    with dst.cursor() as c:
        for d in drives:
            dist = (d["e_odo"] - d["s_odo"]) if (d["e_odo"] is not None and d["s_odo"] is not None) else None
            dur = None
            if d["start_ts"] and d["end_ts"]:
                dur = int((d["end_ts"] - d["start_ts"]).total_seconds())
            avg = (dist / (dur / 3600.0)) if (dist and dur and dur > 0) else None
            c.execute("""INSERT INTO drives (vin,start_ts,end_ts,duration_s,start_odometer,end_odometer,
                         distance_km,start_soc,end_soc,soc_used,start_lat,start_lng,end_lat,end_lng,
                         max_speed,avg_speed,outside_temp,source)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
                         ON DUPLICATE KEY UPDATE end_ts=VALUES(end_ts)""",
                      (VIN, d["start_ts"], d["end_ts"], dur, d["s_odo"], d["e_odo"], dist,
                       d["s_soc"], d["e_soc"],
                       (d["s_soc"] - d["e_soc"]) if (d["s_soc"] is not None and d["e_soc"] is not None) else None,
                       d["s_lat"], d["s_lng"], d["e_lat"], d["e_lng"], None, avg, d["otemp"]))
            n += 1
    print("drives backfilled: %d" % n)

    # ---- charges ----
    print("fetching charges from teslalogger...", flush=True)
    with src.cursor() as c:
        c.execute("""
          SELECT cs.StartDate AS start_ts, cs.EndDate AS end_ts,
                 c1.battery_level AS s_soc, c2.battery_level AS e_soc,
                 c1.charge_energy_added AS s_e, c2.charge_energy_added AS e_e,
                 p.lat AS lat, p.lng AS lng,
                 (SELECT MAX(charger_power) FROM charging
                    WHERE CarID=cs.CarID AND id BETWEEN cs.StartChargingID AND cs.EndChargingID) AS max_power
          FROM chargingstate cs
          JOIN charging c1 ON c1.id = cs.StartChargingID
          JOIN charging c2 ON c2.id = cs.EndChargingID
          LEFT JOIN pos p ON p.id = cs.Pos
          WHERE cs.CarID=%s AND cs.EndDate IS NOT NULL
        """, (car_id,))
        charges = c.fetchall()
    m = 0
    with dst.cursor() as c:
        for ch in charges:
            dur = None
            if ch["start_ts"] and ch["end_ts"]:
                dur = int((ch["end_ts"] - ch["start_ts"]).total_seconds())
            kwh = (ch["e_e"] - ch["s_e"]) if (ch["e_e"] is not None and ch["s_e"] is not None) else None
            ctype = "DC" if (ch["max_power"] and ch["max_power"] > 50) else "AC"
            c.execute("""INSERT INTO charges (vin,start_ts,end_ts,duration_s,start_soc,end_soc,soc_added,
                         energy_added_kwh,max_power_kw,charger_type,lat,lng,source)
                         VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
                         ON DUPLICATE KEY UPDATE end_ts=VALUES(end_ts)""",
                      (VIN, ch["start_ts"], ch["end_ts"], dur, ch["s_soc"], ch["e_soc"],
                       (ch["e_soc"] - ch["s_soc"]) if (ch["e_soc"] is not None and ch["s_soc"] is not None) else None,
                       kwh, ch["max_power"], ctype, ch["lat"], ch["lng"]))
            m += 1
    print("charges backfilled: %d" % m)
    print("done.")


if __name__ == "__main__":
    main()
