#!/usr/bin/env python3
"""
Clean up geomap tooltips on the migrated dashboards: teslalogger builds an HTML 'address'
column for its old map tooltip, which native geomap renders as raw markup. This wraps each
geomap query to strip HTML tags (and aliases it to 'info'), pins the location to the lat/lng
columns, converts dense position-history marker maps to thin route layers, and adds parked /
charging landmark layers. Idempotent (marker comment). Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python migration/grafana-clean-maps.py
"""
import os
import re
from copy import deepcopy

from _grafana import folder_uid, for_each_dashboard, walk_panels

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
MARK = "/*html_stripped*/"
FILT = "/*coords_filtered*/"
LAND = "/*landmarks_added*/"
MARKER_COLOR = os.environ.get("MARKER_COLOR", "#F2495C")  # high-contrast red on the OSM basemap
MARKER_SIZE = 6
ROUTE_COLOR = os.environ.get("ROUTE_COLOR", "#E02F44")
ROUTE_WIDTH = float(os.environ.get("ROUTE_WIDTH", "2"))
MAP_FIT_LAYER = os.environ.get("MAP_FIT_LAYER", "Position history")
MAP_FIT_PADDING = float(os.environ.get("MAP_FIT_PADDING", "8"))
MAP_FIT_MAX_ZOOM = float(os.environ.get("MAP_FIT_MAX_ZOOM", "14"))
PARK_COLOR = os.environ.get("PARK_COLOR", "#3274D9")
AC_CHARGE_COLOR = os.environ.get("AC_CHARGE_COLOR", "#FFD400")
DC_CHARGE_COLOR = os.environ.get("DC_CHARGE_COLOR", "#E02F44")
PARK_LABEL_COLOR = os.environ.get("PARK_LABEL_COLOR", "#FFFFFF")
ROUTE_STYLE = {"color": {"fixed": ROUTE_COLOR}, "opacity": 0.75, "lineWidth": ROUTE_WIDTH,
               "size": {"fixed": ROUTE_WIDTH, "min": 1, "max": 4}}
MARKER_STYLE = {"color": {"fixed": MARKER_COLOR}, "size": {"fixed": MARKER_SIZE}, "opacity": 0.9}
PARK_STYLE = {"color": {"fixed": PARK_COLOR}, "opacity": 1,
              "size": {"fixed": 7, "min": 5, "max": 10},
              "lineWidth": 2,
              "symbol": {"fixed": "img/icons/marker/square.svg", "mode": "fixed"}}
PARK_LABEL_STYLE = {"color": {"fixed": PARK_LABEL_COLOR}, "opacity": 1,
                    "text": {"fixed": "P", "mode": "fixed"},
                    "textConfig": {"fontSize": 9, "offsetY": 0}}
AC_CHARGE_STYLE = {"color": {"fixed": AC_CHARGE_COLOR}, "opacity": 1,
                   "text": {"fixed": "⚡", "mode": "fixed"},
                   "textConfig": {"fontSize": 14, "offsetY": 0}}
DC_CHARGE_STYLE = {"color": {"fixed": DC_CHARGE_COLOR}, "opacity": 1,
                   "text": {"fixed": "⚡", "mode": "fixed"},
                   "textConfig": {"fontSize": 14, "offsetY": 0}}
LANDMARK_LAYER_NAMES = {"Parked halo", "Charging halo", "Parked", "Charging",
                        "Parked label", "Charging label", "AC charger", "DC charger"}


def style_set(style, key, value):
    if style.get(key) == value:
        return 0
    style[key] = value
    return 1


def history_query(sql):
    low = " ".join((sql or "").lower().split())
    return (" from pos" in low and "group by" not in low
            and ("order by id" in low or "order by datum" in low or "order by 1" in low))


def route_layer():
    return {"name": MAP_FIT_LAYER,
            "type": "route",
            "location": {"mode": "coords", "latitude": "lat", "longitude": "lng"},
            "config": {"style": dict(ROUTE_STYLE), "arrow": 0},
            "tooltip": False}


def marker_layer(name, ref_id, style):
    return {"name": name,
            "type": "markers",
            "filterData": {"id": "byRefId", "options": ref_id},
            "location": {"mode": "coords", "latitude": "lat", "longitude": "lng"},
            "config": {"showLegend": False, "style": deepcopy(style)},
            "tooltip": True}


def landmark_layers(park_ref, ac_ref, dc_ref):
    return [
        marker_layer("Parked", park_ref, PARK_STYLE),
        marker_layer("AC charger", ac_ref, AC_CHARGE_STYLE),
        marker_layer("DC charger", dc_ref, DC_CHARGE_STYLE),
        marker_layer("Parked label", park_ref, PARK_LABEL_STYLE),
    ]


def ensure_landmark_layers(layers, park_ref, ac_ref, dc_ref):
    keep = [layer for layer in layers if layer.get("name") not in LANDMARK_LAYER_NAMES]
    landmarks = landmark_layers(park_ref, ac_ref, dc_ref)
    routes = [layer for layer in keep if layer.get("type") == "route"]
    other = [layer for layer in keep if layer.get("type") != "route"]
    wanted = other + landmarks[:3] + routes + landmarks[3:]
    if layers == wanted:
        return 0
    layers[:] = wanted
    return 1


def used_refids(targets):
    return {t.get("refId") for t in targets if t.get("refId")}


def next_refid(used, preferred):
    if preferred not in used:
        used.add(preferred)
        return preferred
    for code in "BCDEFGHIJKLMNOPQRSTUVWXYZ":
        if code not in used:
            used.add(code)
            return code
    return None


def mysql_car_filter(sql):
    match = re.search(r"\bcarid\s*=\s*('[^']+'|\$\{?[A-Za-z0-9_]+\}?|[0-9]+)", sql or "", re.I)
    return match.group(0) if match else None


def clone_target(base, ref_id, raw_sql):
    tgt = deepcopy(base)
    tgt["refId"] = ref_id
    tgt["format"] = "table"
    tgt["rawSql"] = raw_sql
    tgt.pop("query", None)
    return tgt


def landmark_targets(base, car_filter, park_ref, ac_ref, dc_ref):
    park_sql = (
        "SELECT ROUND(p.lat,4) AS lat, ROUND(p.lng,4) AS lng, 'Parked' AS kind, "
        "COUNT(*) AS visits, MAX(ds.EndDate) AS last_seen "
        "FROM drivestate ds JOIN pos p ON p.id=ds.EndPos "
        "WHERE ds.%s AND ds.EndPos IS NOT NULL AND $__timeFilter(ds.EndDate) "
        "AND p.lat IS NOT NULL AND p.lng IS NOT NULL AND p.lat<>0 AND p.lng<>0 "
        "GROUP BY 1,2 ORDER BY last_seen DESC LIMIT 200 %s"
        % (car_filter, LAND))
    ac_sql = (
        "SELECT ROUND(p.lat,4) AS lat, ROUND(p.lng,4) AS lng, 'AC' AS kind, "
        "COUNT(*) AS visits, MAX(cs.StartDate) AS last_seen "
        "FROM chargingstate cs JOIN pos p ON p.id=cs.Pos "
        "WHERE cs.%s AND cs.Pos IS NOT NULL AND $__timeFilter(cs.StartDate) "
        "AND COALESCE(cs.fast_charger_type,'')='' "
        "AND p.lat IS NOT NULL AND p.lng IS NOT NULL AND p.lat<>0 AND p.lng<>0 "
        "GROUP BY 1,2 ORDER BY last_seen DESC LIMIT 100 %s"
        % (car_filter, LAND))
    dc_sql = (
        "SELECT ROUND(p.lat,4) AS lat, ROUND(p.lng,4) AS lng, 'DC' AS kind, "
        "COUNT(*) AS visits, MAX(cs.StartDate) AS last_seen "
        "FROM chargingstate cs JOIN pos p ON p.id=cs.Pos "
        "WHERE cs.%s AND cs.Pos IS NOT NULL AND $__timeFilter(cs.StartDate) "
        "AND COALESCE(cs.fast_charger_type,'')<>'' "
        "AND p.lat IS NOT NULL AND p.lng IS NOT NULL AND p.lat<>0 AND p.lng<>0 "
        "GROUP BY 1,2 ORDER BY last_seen DESC LIMIT 100 %s"
        % (car_filter, LAND))
    return [clone_target(base, park_ref, park_sql),
            clone_target(base, ac_ref, ac_sql),
            clone_target(base, dc_ref, dc_sql)]


def ensure_landmark_targets(p, base, car_filter):
    # Rebuild landmark targets instead of appending. This repairs older dashboards that went
    # through multiple cleanup iterations and may now have duplicated landmark refIds.
    targets = p.setdefault("targets", [])
    keep = [t for t in targets if LAND not in (t.get("rawSql") or "")]
    used = used_refids(keep)
    park_ref = next_refid(used, "B")
    ac_ref = next_refid(used, "C")
    dc_ref = next_refid(used, "D")
    if not (park_ref and ac_ref and dc_ref):
        return 0, None, None, None
    wanted = keep + landmark_targets(base, car_filter, park_ref, ac_ref, dc_ref)
    if targets == wanted:
        return 0, park_ref, ac_ref, dc_ref
    p["targets"] = wanted
    return 1, park_ref, ac_ref, dc_ref


def ensure_fit_view(p):
    opts = p.setdefault("options", {})
    want = {"id": "fit", "allLayers": False, "layer": MAP_FIT_LAYER,
            "padding": MAP_FIT_PADDING, "zoom": MAP_FIT_MAX_ZOOM}
    if opts.get("view") == want:
        return 0
    opts["view"] = want
    return 1


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
    # Drop null/zero coordinates (GPS dropouts log as lat=0,lng=0 -> "Null Island" in the
    # Atlantic). teslalogger's old map plugins skipped these; native geomap plots them.
    # Wrap the query once to filter them out. Idempotent via FILT marker.
    for tgt in p.get("targets", []):
        sql = tgt.get("rawSql") or ""
        if ("lat" in sql) and (FILT not in sql) and (LAND not in sql):
            tgt["rawSql"] = ("SELECT * FROM (\n%s\n) cf "
                             "WHERE lat IS NOT NULL AND lng IS NOT NULL AND lat<>0 AND lng<>0 %s"
                             % (sql, FILT))
            tgt["format"] = "table"
            n += 1
    # Dashboards that were already modernized before this script learned about route layers are
    # native geomaps with a single fat marker layer. If the query is a raw ordered pos history,
    # switch that layer to a route so re-running cleanup visibly fixes existing dashboards.
    if any(history_query(t.get("rawSql") or "") for t in p.get("targets", [])):
        opts = p.setdefault("options", {})
        layers = opts.setdefault("layers", [])
        base = next((t for t in p.get("targets", []) if history_query(t.get("rawSql") or "")), None)
        car_filter = mysql_car_filter(base.get("rawSql") or "") if base else None
        n += ensure_fit_view(p)
        if not any(layer.get("type") == "route" for layer in layers):
            if len(layers) == 1 and layers[0].get("type") == "markers":
                layers[0].clear()
                layers[0].update(route_layer())
                n += 1
            elif not layers:
                layers.append(route_layer())
                n += 1
        if base and car_filter:
            changed, park_ref, ac_ref, dc_ref = ensure_landmark_targets(p, base, car_filter)
            n += changed
        else:
            park_ref = ac_ref = dc_ref = None
        if park_ref and ac_ref and dc_ref:
            n += ensure_landmark_layers(layers, park_ref, ac_ref, dc_ref)
    # Per-layer touch-ups (idempotent):
    #  - hide the layer legend ("Layer 1" box): config.showLegend, not a top-level option
    #  - set a high-contrast marker color/size under config.style (the proper nesting)
    #  - keep route/track history thin; Grafana's defaults are much heavier than TeslaLogger's map
    opts = p.setdefault("options", {})
    if "legend" in opts:
        opts.pop("legend"); n += 1
    for layer in opts.get("layers", []):
        ltype = layer.get("type")
        cfg = layer.setdefault("config", {})
        style = cfg.setdefault("style", {})
        if ltype == "markers":
            if cfg.get("showLegend") is not False:
                cfg["showLegend"] = False; n += 1
            if "size" in cfg:  # loose key superseded by style.size
                cfg.pop("size"); n += 1
            want = {"Parked": PARK_STYLE, "Parked label": PARK_LABEL_STYLE,
                    "AC charger": AC_CHARGE_STYLE,
                    "DC charger": DC_CHARGE_STYLE}.get(layer.get("name"), MARKER_STYLE)
            for k, v in want.items():
                n += style_set(style, k, v)
        elif ltype == "route":
            if layer.get("name") != MAP_FIT_LAYER:
                layer["name"] = MAP_FIT_LAYER; n += 1
            if cfg.get("arrow") not in (0, None):
                cfg["arrow"] = 0; n += 1
            for k, v in ROUTE_STYLE.items():
                n += style_set(style, k, v)
            if layer.get("tooltip") is not False:
                layer["tooltip"] = False; n += 1
    return n


def main():
    fu = folder_uid(FOLDER, TOK, DST)
    if not fu:
        print("folder not found"); return
    for_each_dashboard(fu, lambda d: walk_panels(d.get("panels", []), clean_panel),
                       TOK, DST, "  %s: cleaned %d map(s) -> %s")


if __name__ == "__main__":
    main()
