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

The panel must also carry pluginVersion >= 11.1: Grafana's xyChartMigrationHandler
re-runs the beta->GA migration whenever pluginVersion is missing or older (API-posted
dashboards never get one stamped), which mangles already-GA options into invalid
matchers and renders "Err".

Finally, a field mapped to a series' color needs a by-value color scheme (continuous /
thresholds / value-mappings). teslalogger colors charging curves by chargingstate id and
relies on Grafana 10's beta renderer; the GA renderer's fieldValueColors() builds an empty
palette for the default palette-classic mode, so point drawing throws alpha(undefined) and
the chart renders with axes but no points. We add a continuous color override per color
field that lacks one.

Idempotent: panels already in the GA shape with a pluginVersion and color override are left
untouched. Run in the tools container (this stack's Grafana listens on :3003):
  docker exec -e DST_GRAFANA_TOKEN=... -e DST_GRAFANA=http://grafana:3003 \
    tesla-tools python migration/grafana-fix-xychart.py
"""
import os

from _grafana import api, folder_uid, for_each_dashboard, search_dashboards, walk_panels

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
COLOR_MODE = os.environ.get("XY_COLOR_MODE", "continuous-GrYlRd")  # by-value scheme for color fields
GRAFANA_VERSION = None  # resolved in main(); fix_panel falls back to 11.1.0


def by_name(field):
    return {"matcher": {"id": "byName", "options": field}}


def color_field_name(s):
    # the field name a GA series colors by, or None
    m = (s.get("color") or {}).get("matcher") or {}
    return m.get("options") if m.get("id") == "byName" else None


def ensure_color_modes(p):
    # every field used as a series color needs a by-value color scheme, or the GA renderer
    # builds an empty palette and crashes drawing points (alpha(undefined)).
    fields = {color_field_name(s) for s in p.get("options", {}).get("series", [])} - {None}
    if not fields:
        return 0
    fc = p.setdefault("fieldConfig", {"defaults": {}, "overrides": []})
    overrides = fc.setdefault("overrides", [])
    changed = 0
    for f in fields:
        ov = next((o for o in overrides
                   if o.get("matcher", {}).get("id") == "byName"
                   and o["matcher"].get("options") == f), None)
        if ov is None:
            ov = {"matcher": {"id": "byName", "options": f}, "properties": []}
            overrides.append(ov)
        props = ov.setdefault("properties", [])
        if any(pr.get("id") == "color" for pr in props):
            continue   # a color mode is already set, leave it
        props.append({"id": "color", "value": {"mode": COLOR_MODE}})
        changed = 1
    return changed


def fix_panel(p):
    if p.get("type") != "xychart":
        return 0
    changed = 0
    # stamp pluginVersion so Grafana skips xyChartMigrationHandler (it fires when the
    # version is missing or < 11.1 and corrupts options that are already GA-shaped)
    try:
        pv = float(".".join(str(p.get("pluginVersion") or "0").split(".")[:2]))
    except ValueError:
        pv = 0.0
    if pv < 11.1:
        p["pluginVersion"] = GRAFANA_VERSION or "11.1.0"
        changed = 1
    o = p.get("options", {})
    series = o.get("series", [])
    beta = ("seriesMapping" in o or "dims" in o
            or any(isinstance(s.get("x"), str) or isinstance(s.get("y"), str) for s in series))
    if not beta:
        # GA-shaped already, but manual mapping REQUIRES a frame matcher: prepSeries()
        # silently skips any series without one and the panel renders "Err"
        if o.get("mapping", "manual") == "manual":
            for s in series:
                if "frame" not in s:
                    s["frame"] = {"matcher": {"id": "byIndex", "options": 0}}
                    changed = 1
        return changed | ensure_color_modes(p)
    new_series = []
    for s in series:
        # frame matcher is mandatory in manual mapping (beta stored a plain index or nothing)
        ns = {"frame": {"matcher": {"id": "byIndex",
                                    "options": s["frame"] if isinstance(s.get("frame"), int) else 0}}}
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
    ensure_color_modes(p)
    return 1


def main():
    global GRAFANA_VERSION
    GRAFANA_VERSION = api("GET", "/api/health", TOK, DST).json().get("version") or "11.1.0"
    fu = folder_uid(FOLDER, TOK, DST)
    if not fu:
        print("folder '%s' not found" % FOLDER); return
    items = search_dashboards(fu, TOK, DST)
    print("%d dashboards in '%s'" % (len(items), FOLDER))
    for_each_dashboard(fu, lambda d: walk_panels(d.get("panels", []), fix_panel),
                       TOK, DST, "  %s: migrated %d xychart panel(s) -> %s", items=items)


if __name__ == "__main__":
    main()
