#!/usr/bin/env python3
"""
Modernize migrated teslalogger dashboards for Grafana 12+/13 (no Angular): swap dead
panel types to native equivalents in place. Targets only the given folder so your other
dashboards are untouched. Queries carry over unchanged; native panels apply sane defaults.

  graph                  -> timeseries
  grafana-piechart-panel -> piechart
  natel-discrete-panel   -> state-timeline
  grafana-worldmap-panel -> geomap   (markers, auto lat/lng)
  pr0ps-trackmap-panel   -> geomap   (markers, auto lat/lng)

Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python scripts/grafana-modernize.py
"""
import os
import requests

DST = os.environ.get("DST_GRAFANA", "http://grafana:3003").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
h = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"}

CONV = {
    "graph": "timeseries",
    "grafana-piechart-panel": "piechart",
    "natel-discrete-panel": "state-timeline",
    "grafana-worldmap-panel": "geomap",
    "pr0ps-trackmap-panel": "geomap",
}


def convert(panels):
    n = 0
    for p in panels:
        t = p.get("type")
        if t in CONV:
            nt = CONV[t]
            p["type"] = nt
            if nt == "geomap":
                p["options"] = {"basemap": {"type": "osm-standard"}, "view": {"id": "fit"},
                                "layers": [{"type": "markers", "location": {"mode": "auto"},
                                            "config": {"size": {"fixed": 3}}}]}
            elif nt == "piechart":
                p.pop("options", None)
            n += 1
        if "panels" in p:
            n += convert(p["panels"])
    return n


def main():
    folder_uid = None
    for f in requests.get("%s/api/folders" % DST, headers=h, timeout=30).json():
        if f.get("title") == FOLDER:
            folder_uid = f.get("uid")
    if not folder_uid:
        print("folder '%s' not found" % FOLDER); return
    items = requests.get("%s/api/search?type=dash-db&folderUIDs=%s" % (DST, folder_uid), headers=h, timeout=30).json()
    print("%d dashboards in '%s'" % (len(items), FOLDER))
    for it in items:
        full = requests.get("%s/api/dashboards/uid/%s" % (DST, it["uid"]), headers=h, timeout=30).json()
        dash = full["dashboard"]
        c = convert(dash.get("panels", []))
        if c:
            r = requests.post("%s/api/dashboards/db" % DST, headers=h, timeout=60,
                              json={"dashboard": dash, "overwrite": True, "folderUid": folder_uid})
            print("  %s: converted %d panel(s) -> %s" % (dash.get("title"), c, r.status_code))
        else:
            print("  %s: already native" % dash.get("title"))


if __name__ == "__main__":
    main()
