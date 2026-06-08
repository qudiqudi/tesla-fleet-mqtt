#!/usr/bin/env python3
"""
Clean up geomap tooltips on the migrated dashboards: teslalogger builds an HTML 'address'
column for its old map tooltip, which native geomap renders as raw markup. This wraps each
geomap query to strip HTML tags (and aliases it to 'info'), and pins the marker location to
the lat/lng columns. Idempotent (marker comment). Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python migration/grafana-clean-maps.py
"""
import os
import requests

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
h = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"}
MARK = "/*html_stripped*/"
MARKER_COLOR = os.environ.get("MARKER_COLOR", "#F2495C")  # high-contrast red on the OSM basemap
MARKER_SIZE = 6


def clean_panel(p):
    if p.get("type") != "geomap":
        return 0
    n = 0
    for tgt in p.get("targets", []):
        sql = tgt.get("rawSql") or ""
        if "address" in sql and MARK not in sql:
            cols = "lat, lng, " + ("type, " if " type" in sql or " as type" in sql.lower() else "")
            tgt["rawSql"] = ("SELECT %s REGEXP_REPLACE(address, '<[^>]+>', '') AS info %s\nFROM (\n%s\n) q"
                             % (cols, MARK, sql))
            tgt["format"] = "table"
            n += 1
    if n:
        p["options"] = {"basemap": {"type": "osm-standard"}, "view": {"id": "fit"},
                        "layers": [{"type": "markers",
                                    "location": {"mode": "coords", "latitude": "lat", "longitude": "lng"},
                                    "config": {"showLegend": False},
                                    "tooltip": True}]}
    # Per-layer touch-ups (idempotent):
    #  - hide the layer legend ("Layer 1" box): config.showLegend, not a top-level option
    #  - set a high-contrast marker color/size under config.style (the proper nesting)
    opts = p.setdefault("options", {})
    if "legend" in opts:
        opts.pop("legend"); n += 1
    for layer in opts.get("layers", []):
        if layer.get("type") != "markers":
            continue
        cfg = layer.setdefault("config", {})
        if cfg.get("showLegend") is not False:
            cfg["showLegend"] = False; n += 1
        if "size" in cfg:  # loose key superseded by style.size
            cfg.pop("size"); n += 1
        style = cfg.setdefault("style", {})
        want = {"color": {"fixed": MARKER_COLOR}, "size": {"fixed": MARKER_SIZE}, "opacity": 0.9}
        for k, v in want.items():
            if style.get(k) != v:
                style[k] = v; n += 1
    return n


def walk(panels):
    n = 0
    for p in panels:
        n += clean_panel(p)
        if "panels" in p:
            n += walk(p["panels"])
    return n


def main():
    folder_uid = None
    for f in requests.get("%s/api/folders" % DST, headers=h, timeout=30).json():
        if f.get("title") == FOLDER:
            folder_uid = f.get("uid")
    if not folder_uid:
        print("folder not found"); return
    items = requests.get("%s/api/search?type=dash-db&folderUIDs=%s" % (DST, folder_uid), headers=h, timeout=30).json()
    for it in items:
        full = requests.get("%s/api/dashboards/uid/%s" % (DST, it["uid"]), headers=h, timeout=30).json()
        dash = full["dashboard"]
        c = walk(dash.get("panels", []))
        if c:
            r = requests.post("%s/api/dashboards/db" % DST, headers=h, timeout=60,
                              json={"dashboard": dash, "overwrite": True, "folderUid": folder_uid})
            print("  %s: cleaned %d map(s) -> %s" % (dash.get("title"), c, r.status_code))


if __name__ == "__main__":
    main()
