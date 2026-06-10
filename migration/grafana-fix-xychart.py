#!/usr/bin/env python3
"""
Migrate XY chart panels from the beta options schema to the GA one (Grafana 11+).

teslalogger's "Charging Curves" panels were built on Grafana 10, where the xychart
panel was beta and configured as

  {"seriesMapping": "manual", "dims": {...},
   "series": [{"x": "battery_level", "y": "charger_power", "pointColor": {"field": "color"}}]}

The GA panel (Grafana >= 11) expects matcher-based series and silently renders an
empty chart for the old shape (the query still returns data):

  {"mapping": "manual",
   "series": [{"x": {"matcher": {"id": "byName", "options": "battery_level"}},
               "y": {"matcher": {"id": "byName", "options": "charger_power"}},
               "color": {"matcher": {"id": "byName", "options": "color"}}}]}

Idempotent: panels already in the GA shape are left untouched. Run in the tools
container (this stack's Grafana listens on :3003):
  docker exec -e DST_GRAFANA_TOKEN=... -e DST_GRAFANA=http://grafana:3003 \
    tesla-tools python migration/grafana-fix-xychart.py
"""
import os

from _grafana import folder_uid, for_each_dashboard, search_dashboards, walk_panels

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")


def by_name(field):
    return {"matcher": {"id": "byName", "options": field}}


def fix_panel(p):
    if p.get("type") != "xychart":
        return 0
    o = p.get("options", {})
    series = o.get("series", [])
    beta = ("seriesMapping" in o or "dims" in o
            or any(isinstance(s.get("x"), str) or isinstance(s.get("y"), str) for s in series))
    if not beta:
        return 0
    new_series = []
    for s in series:
        ns = {}
        for axis in ("x", "y"):
            v = s.get(axis)
            if isinstance(v, str):
                ns[axis] = by_name(v)
            elif isinstance(v, dict):
                ns[axis] = v
        # beta pointColor/pointSize {"field": ...} -> GA color/size matchers
        for old, new in (("pointColor", "color"), ("pointSize", "size")):
            v = s.get(old)
            if isinstance(v, dict) and v.get("field"):
                ns[new] = by_name(v["field"])
        if isinstance(s.get("name"), str):
            ns["name"] = {"fixed": s["name"]}
        new_series.append(ns)
    # a beta panel without explicit series still carried the x dim in dims.x
    if not new_series and isinstance(o.get("dims"), dict) and o["dims"].get("x"):
        new_series = [{"x": by_name(o["dims"]["x"])}]
    o["mapping"] = o.pop("seriesMapping", "manual")
    o["series"] = new_series
    o.pop("dims", None)
    p["options"] = o
    return 1


def main():
    fu = folder_uid(FOLDER, TOK, DST)
    if not fu:
        print("folder '%s' not found" % FOLDER); return
    items = search_dashboards(fu, TOK, DST)
    print("%d dashboards in '%s'" % (len(items), FOLDER))
    for_each_dashboard(fu, lambda d: walk_panels(d.get("panels", []), fix_panel),
                       TOK, DST, "  %s: migrated %d xychart panel(s) -> %s", items=items)


if __name__ == "__main__":
    main()
