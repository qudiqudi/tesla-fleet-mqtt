#!/usr/bin/env python3
"""
Clean up geomap tooltips on the migrated dashboards: teslalogger builds an HTML 'address'
column for its old map tooltip, which native geomap renders as raw markup. This wraps each
geomap query to strip HTML tags (and aliases it to 'info'), pins the location to the lat/lng
columns, converts dense position-history marker maps to thin route layers, adds parked /
charging landmark layers, and rebuilds teslalogger's "Visited" map (track + chargers in one
UNION) into a route line plus charger pins. Idempotent (marker comment). Run in the tools container:
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
VISITED_MARKER_SIZE = float(os.environ.get("VISITED_MARKER_SIZE", "3"))
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
VISITED_MARKER_STYLE = {"color": {"fixed": MARKER_COLOR},
                        "size": {"fixed": VISITED_MARKER_SIZE, "min": 2, "max": 6},
                        "opacity": 0.75}
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
# teslalogger's "Visited" map is a single UNION query returning an averaged GPS track (type=0)
# alongside charger locations (type=1 Supercharger, type=2 other fast DC). Rendered as one marker
# layer the track collapses into a blob of dots; teslalogger draws the track as a connected line
# and the chargers as labelled pins. We rebuild it that way.
#
# Grafana 13's geomap recolours any .svg marker to a single tint and a marker's text label inherits
# the marker colour, so a "coloured pin + white glyph" needs to be composed from stacked layers.
# Built-in marker SVGs are thin outline shapes (cheap-looking when tinted) except `circle`, which is
# special-cased to a crisp vector disc. So each charger is three stacked layers: a white halo disc
# (the pin's border), a coloured disc on top, and a white glyph (T for Superchargers, a bolt for
# other fast DC) -- a clean, high-contrast marker that holds up on the busy OSM basemap.
VISITED_TL = "/*tl_visited*/"
VISITED_ROUTE_COLOR = os.environ.get("VISITED_ROUTE_COLOR", "#2D7BFF")  # teslalogger-style track blue
VISITED_ROUTE_WIDTH = float(os.environ.get("VISITED_ROUTE_WIDTH", "3"))
VISITED_SC_COLOR = os.environ.get("VISITED_SC_COLOR", "#E02F44")  # supercharger disc (red)
VISITED_DC_COLOR = os.environ.get("VISITED_DC_COLOR", "#56A64B")  # other fast-charger disc (green)
VISITED_HALO_COLOR = os.environ.get("VISITED_HALO_COLOR", "#FFFFFF")  # pin border
VISITED_GLYPH_COLOR = os.environ.get("VISITED_GLYPH_COLOR", "#FFFFFF")
VISITED_SC_GLYPH = os.environ.get("VISITED_SC_GLYPH", "T")  # Tesla Supercharger
VISITED_DC_GLYPH = os.environ.get("VISITED_DC_GLYPH", "⚡")  # other fast charger
VISITED_DOT_SIZE = float(os.environ.get("VISITED_DOT_SIZE", "8"))
VISITED_HALO_SIZE = VISITED_DOT_SIZE + float(os.environ.get("VISITED_HALO_WIDTH", "3"))
VISITED_DISC = {"fixed": "img/icons/marker/circle.svg", "mode": "fixed"}  # crisp vector circle
VISITED_ROUTE_STYLE = {"color": {"fixed": VISITED_ROUTE_COLOR}, "opacity": 0.9,
                       "lineWidth": VISITED_ROUTE_WIDTH, "size": {"fixed": 2, "min": 1, "max": 4}}
VISITED_HALO_STYLE = {"color": {"fixed": VISITED_HALO_COLOR}, "opacity": 1, "lineWidth": 1,
                      "size": {"fixed": VISITED_HALO_SIZE}, "symbol": dict(VISITED_DISC)}


def _visited_disc(color):
    return {"color": {"fixed": color}, "opacity": 1, "lineWidth": 1,
            "size": {"fixed": VISITED_DOT_SIZE}, "symbol": dict(VISITED_DISC)}


def _visited_glyph(text, font_size):
    return {"color": {"fixed": VISITED_GLYPH_COLOR}, "opacity": 1,
            "text": {"fixed": text, "mode": "fixed"},
            "textConfig": {"fontSize": font_size, "offsetY": 0}}


VISITED_SC_DISC_STYLE = _visited_disc(VISITED_SC_COLOR)
VISITED_DC_DISC_STYLE = _visited_disc(VISITED_DC_COLOR)
VISITED_SC_GLYPH_STYLE = _visited_glyph(VISITED_SC_GLYPH, 11)
VISITED_DC_GLYPH_STYLE = _visited_glyph(VISITED_DC_GLYPH, 13)


def style_set(style, key, value):
    if style.get(key) == value:
        return 0
    style[key] = value
    return 1


def history_query(sql):
    low = " ".join((sql or "").lower().split())
    return (" from pos" in low and "group by" not in low
            and ("order by id" in low or "order by datum" in low or "order by 1" in low))


def coords_query(sql):
    low = (sql or "").lower()
    return "lat" in low and "lng" in low


def visited_query(sql):
    low = " ".join((sql or "").lower().split())
    return coords_query(low) and ("group by" in low or " count(" in low or "avg(" in low)


def tl_visited_query(sql):
    # teslalogger's Visited UNION query: averaged track + chargers, discriminated by a `type` column.
    low = " ".join((sql or "").lower().split())
    return coords_query(low) and " as type" in low and " union " in low and "from pos" in low


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


def ensure_fit_view(p, layer=None):
    opts = p.setdefault("options", {})
    if layer:
        want = {"id": "fit", "allLayers": False, "layer": layer,
                "padding": MAP_FIT_PADDING, "zoom": MAP_FIT_MAX_ZOOM}
    else:
        want = {"id": "fit", "allLayers": True,
                "padding": MAP_FIT_PADDING, "zoom": MAP_FIT_MAX_ZOOM}
    if opts.get("view") == want:
        return 0
    opts["view"] = want
    return 1


def charger_layers(label, ref_id, disc_style, glyph_style):
    # A teslalogger-style pin, stacked bottom-to-top: white halo (border), coloured disc, white
    # glyph. Only the disc carries the tooltip so a click shows one charging popup, not three.
    halo = marker_layer(label + " halo", ref_id, VISITED_HALO_STYLE)
    halo["tooltip"] = False
    disc = marker_layer(label + "s", ref_id, disc_style)
    glyph = marker_layer(label + " label", ref_id, glyph_style)
    glyph["tooltip"] = False
    return [halo, disc, glyph]


def tl_visited_panel(p):
    # Split the single Visited UNION into a track source (kept as-is) and two charger-only
    # companion queries (type=1 Supercharger, type=2 other DC), then render a blue route layer
    # over the track plus stacked pin layers per charger type. Idempotent via the VISITED_TL marker.
    targets = p.get("targets", [])
    base = next((t for t in targets
                 if tl_visited_query(t.get("rawSql") or "") and VISITED_TL not in (t.get("rawSql") or "")),
                None)
    if not base:
        return 0
    base_ref = base.get("refId")
    base_sql = base.get("rawSql") or ""
    keep = [t for t in targets if VISITED_TL not in (t.get("rawSql") or "")]
    used = used_refids(keep)
    sc_ref = next_refid(used, "B")
    dc_ref = next_refid(used, "C")
    if not (base_ref and sc_ref and dc_ref):
        return 0
    sc_sql = "SELECT * FROM (\n%s\n) tlv WHERE type = 1 %s" % (base_sql, VISITED_TL)
    dc_sql = "SELECT * FROM (\n%s\n) tlv WHERE type = 2 %s" % (base_sql, VISITED_TL)
    wanted_targets = keep + [clone_target(base, sc_ref, sc_sql),
                             clone_target(base, dc_ref, dc_sql)]
    n = 0
    if targets != wanted_targets:
        p["targets"] = wanted_targets
        n += 1
    route = route_layer()
    route["filterData"] = {"id": "byRefId", "options": base_ref}
    route["config"]["style"] = dict(VISITED_ROUTE_STYLE)
    wanted_layers = [route]
    wanted_layers += charger_layers("Supercharger", sc_ref, VISITED_SC_DISC_STYLE, VISITED_SC_GLYPH_STYLE)
    wanted_layers += charger_layers("Fast charger", dc_ref, VISITED_DC_DISC_STYLE, VISITED_DC_GLYPH_STYLE)
    opts = p.setdefault("options", {})
    if opts.get("layers") != wanted_layers:
        opts["layers"] = wanted_layers
        n += 1
    n += ensure_fit_view(p, MAP_FIT_LAYER)
    return n


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
    # teslalogger's "Visited" map (averaged track + chargers in one UNION, discriminated by `type`)
    # renders as a single marker layer -> the track becomes a blob of dots. Rebuild it the
    # teslalogger way: a blue route line for the track plus charger pins. Handle it before the
    # generic branches below, which would otherwise re-style it as a flat marker map.
    if any(tl_visited_query(t.get("rawSql") or "") for t in p.get("targets", [])):
        return n + tl_visited_panel(p)
    # Dashboards that were already modernized before this script learned about route layers are
    # native geomaps with a single fat marker layer. If the query is a raw ordered pos history,
    # switch that layer to a route so re-running cleanup visibly fixes existing dashboards.
    if any(history_query(t.get("rawSql") or "") for t in p.get("targets", [])):
        opts = p.setdefault("options", {})
        layers = opts.setdefault("layers", [])
        base = next((t for t in p.get("targets", []) if history_query(t.get("rawSql") or "")), None)
        car_filter = mysql_car_filter(base.get("rawSql") or "") if base else None
        n += ensure_fit_view(p, MAP_FIT_LAYER)
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
    elif any(coords_query(t.get("rawSql") or "") for t in p.get("targets", [])):
        n += ensure_fit_view(p)
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
            if any(visited_query(t.get("rawSql") or "") for t in p.get("targets", [])):
                default_marker_style = VISITED_MARKER_STYLE
            else:
                default_marker_style = MARKER_STYLE
            want = {"Parked": PARK_STYLE, "Parked label": PARK_LABEL_STYLE,
                    "AC charger": AC_CHARGE_STYLE,
                    "DC charger": DC_CHARGE_STYLE}.get(layer.get("name"), default_marker_style)
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
