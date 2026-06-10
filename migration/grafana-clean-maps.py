#!/usr/bin/env python3
"""
Clean up geomap tooltips on the migrated dashboards: teslalogger builds an HTML 'address'
column for its old map tooltip, which native geomap renders as raw markup. This wraps each
geomap query to strip HTML tags (and aliases it to 'info'), pins the location to the lat/lng
columns, and converts dense position-history marker maps to thin route layers. Idempotent
(marker comment). Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python migration/grafana-clean-maps.py
"""
import os

from _grafana import folder_uid, for_each_dashboard, walk_panels

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
MARK = "/*html_stripped*/"
FILT = "/*coords_filtered*/"
MARKER_COLOR = os.environ.get("MARKER_COLOR", "#F2495C")  # high-contrast red on the OSM basemap
MARKER_SIZE = 6
ROUTE_COLOR = os.environ.get("ROUTE_COLOR", "#E02F44")
ROUTE_WIDTH = float(os.environ.get("ROUTE_WIDTH", "2"))
ROUTE_STYLE = {"color": {"fixed": ROUTE_COLOR}, "opacity": 0.75, "lineWidth": ROUTE_WIDTH,
               "size": {"fixed": ROUTE_WIDTH, "min": 1, "max": 4}}
MARKER_STYLE = {"color": {"fixed": MARKER_COLOR}, "size": {"fixed": MARKER_SIZE}, "opacity": 0.9}


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
    return {"type": "route",
            "location": {"mode": "coords", "latitude": "lat", "longitude": "lng"},
            "config": {"style": dict(ROUTE_STYLE), "arrow": 0},
            "tooltip": False}


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
        if ("lat" in sql) and (FILT not in sql):
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
        if not any(layer.get("type") == "route" for layer in layers):
            if len(layers) == 1 and layers[0].get("type") == "markers":
                layers[0].clear()
                layers[0].update(route_layer())
                n += 1
            elif not layers:
                layers.append(route_layer())
                n += 1
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
            for k, v in MARKER_STYLE.items():
                n += style_set(style, k, v)
        elif ltype == "route":
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
