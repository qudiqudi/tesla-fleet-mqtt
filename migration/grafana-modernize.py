#!/usr/bin/env python3
"""
Modernize migrated teslalogger dashboards for Grafana 12+/13 (Angular removed): convert dead
Angular panel types to native equivalents AND carry their display config across, so units,
legends, series aliases and value mappings survive the move. Targets only the given folder.

A bare type-swap is not enough: native panels read display config from `fieldConfig`/`options`,
while the old Angular panels kept it in their own keys (yaxes, seriesOverrides, aliasColors,
legend, valueMaps, colorMaps). This translates those keys, then drops them.

  graph                  -> timeseries     (yaxes->unit/axis, seriesOverrides+aliasColors->overrides, legend->options)
  natel-discrete-panel   -> state-timeline  (valueMaps+colorMaps -> fieldConfig value mappings)
  grafana-piechart-panel -> piechart        (legend + pieType)
  grafana-worldmap-panel -> geomap          (markers on lat/lng)
  pr0ps-trackmap-panel   -> geomap          (markers on lat/lng)

Detection keys off the leftover Angular keys, so it also repairs panels a previous crude
swap already retyped (type already native but fieldConfig empty). Idempotent.

Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python migration/grafana-modernize.py
"""
import os
import requests

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
h = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"}

# Angular graph keys we translate then strip.
GRAPH_KEYS = ("yaxes", "xaxis", "seriesOverrides", "aliasColors", "legend", "lines",
              "linewidth", "fill", "fillGradient", "points", "pointradius", "bars",
              "stack", "steppedLine", "nullPointMode", "dashes", "spaceLength",
              "percentage", "thresholds", "tooltip", "renderer", "hiddenSeries")
DISCRETE_KEYS = ("valueMaps", "colorMaps", "rangeMaps", "units", "rowHeight", "textSize",
                 "textSizeTime", "showLegend", "showLegendNames", "showLegendValues",
                 "showLegendPercent", "showLegendCounts", "legendSortBy", "metricNameColor",
                 "valueTextColor", "backgroundColor", "crosshairColor", "lineColor",
                 "timeTextColor", "highlightOnMouseover", "extendLastValue", "writeAllValues",
                 "writeLastValue", "writeMetricNames", "showDistinctCount", "showTransitionCount",
                 "expandFromQueryS", "showTimeAxis", "useTimePrecision", "timePrecision",
                 "timeOptions", "display", "legendPercentDecimals")
PIE_KEYS = ("pieType", "legendType", "legend", "valueName", "strokeWidth", "fontSize",
            "format", "combine", "aliasColors", "breakPoint", "sortOrder")
MAP_KEYS = ("mapControl", "lineColor", "pointColor", "maxDataPoints", "lat", "lon", "zoom",
            "showLayerChanger", "scrollWheelZoom", "showZoomControl", "autoZoom", "autoPanLabels")


def strip(p, keys):
    for k in keys:
        p.pop(k, None)


def fc(p):
    f = p.setdefault("fieldConfig", {})
    f.setdefault("defaults", {})
    f.setdefault("overrides", [])
    return f


def graph_to_timeseries(p):
    f = fc(p)
    d = f["defaults"]
    cust = d.setdefault("custom", {})
    yaxes = p.get("yaxes") or []
    if yaxes:
        y0 = yaxes[0] or {}
        if y0.get("format"):
            d["unit"] = y0["format"]
        if y0.get("decimals") is not None:
            d["decimals"] = y0["decimals"]
        if y0.get("label"):
            cust["axisLabel"] = y0["label"]
        if y0.get("min") not in (None, ""):
            d["min"] = _num(y0["min"])
        if y0.get("max") not in (None, ""):
            d["max"] = _num(y0["max"])
        if y0.get("logBase", 1) and y0["logBase"] > 1:
            cust["scaleDistribution"] = {"type": "log", "log": y0["logBase"]}
    # draw style from lines/bars/points
    if p.get("bars"):
        cust["drawStyle"] = "bars"
    elif p.get("points") and not p.get("lines", True):
        cust["drawStyle"] = "points"
    else:
        cust["drawStyle"] = "line"
    if p.get("steppedLine"):
        cust["lineInterpolation"] = "stepAfter"
    if p.get("linewidth") is not None:
        cust["lineWidth"] = p["linewidth"]
    if p.get("pointradius") is not None:
        cust["pointSize"] = max(1, p["pointradius"] + 2)
    fillv = p.get("fill")
    cust["fillOpacity"] = (fillv * 10) if isinstance(fillv, (int, float)) else 10
    if p.get("stack"):
        cust["stacking"] = {"mode": "normal", "group": "A"}
    npm = p.get("nullPointMode")
    if npm == "null":
        cust["spanNulls"] = False
    elif npm == "connected":
        cust["spanNulls"] = True
    # second y-axis unit, applied later to series routed to yaxis 2
    y2unit = yaxes[1].get("format") if len(yaxes) > 1 and yaxes[1] else None

    overrides = f["overrides"]
    for so in (p.get("seriesOverrides") or []):
        alias = so.get("alias")
        if not alias:
            continue
        props = []
        if so.get("yaxis") == 2:
            props.append({"id": "custom.axisPlacement", "value": "right"})
            if y2unit:
                props.append({"id": "unit", "value": y2unit})
        if so.get("fill") is not None:
            props.append({"id": "custom.fillOpacity", "value": so["fill"] * 10})
        if so.get("color"):
            props.append({"id": "color", "value": {"mode": "fixed", "fixedColor": so["color"]}})
        if so.get("dashes"):
            props.append({"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}})
        if so.get("linewidth") is not None:
            props.append({"id": "custom.lineWidth", "value": so["linewidth"]})
        if so.get("bars"):
            props.append({"id": "custom.drawStyle", "value": "bars"})
        if so.get("points"):
            props.append({"id": "custom.drawStyle", "value": "points"})
        if so.get("stack") is False:
            props.append({"id": "custom.stacking", "value": {"mode": "none"}})
        if props:
            overrides.append({"matcher": {"id": "byName", "options": alias}, "properties": props})
    for name, color in (p.get("aliasColors") or {}).items():
        overrides.append({"matcher": {"id": "byName", "options": name},
                          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]})

    lg = p.get("legend") or {}
    if lg:
        calcs = []
        for k, c in (("min", "min"), ("max", "max"), ("avg", "mean"),
                     ("current", "lastNotNull"), ("total", "sum")):
            if lg.get(k):
                calcs.append(c)
        opts = p.setdefault("options", {})
        opts["legend"] = {
            "displayMode": "table" if lg.get("alignAsTable") else "list",
            "placement": "right" if lg.get("rightSide") else "bottom",
            "showLegend": lg.get("show", True),
            "calcs": calcs if lg.get("values") else [],
        }
        opts.setdefault("tooltip", {"mode": "multi", "sort": "none"})
    p["type"] = "timeseries"
    strip(p, GRAPH_KEYS)


def discrete_to_state_timeline(p):
    f = fc(p)
    d = f["defaults"]
    # Build value mappings from the old discrete maps, only if not already done.
    if ("valueMaps" in p) and not d.get("mappings"):
        color_by_text = {c.get("text"): c.get("color") for c in (p.get("colorMaps") or [])}
        options = {}
        for i, vm in enumerate(p.get("valueMaps") or []):
            val = vm.get("value")
            if val is None:
                continue
            opt = {"text": vm.get("text"), "index": i}
            col = color_by_text.get(vm.get("text"))
            if col:
                opt["color"] = col
            options[str(val)] = opt
        mappings = []
        if options:
            mappings.append({"type": "value", "options": options})
        for rm in (p.get("rangeMaps") or []):
            if rm.get("from") == "null" or rm.get("to") == "null":
                mappings.append({"type": "special", "options": {
                    "match": "null", "result": {"text": rm.get("text", "N/A"), "index": len(mappings)}}})
                break
        d["mappings"] = mappings
    d.setdefault("custom", {})["fillOpacity"] = 80
    # Segment color + label come from the value mappings. A forced "thresholds" color mode
    # paints the whole series with the threshold base instead, so drop it.
    d.pop("color", None)
    d.pop("thresholds", None)
    # No legend / no pagination: these panels are short (h~4) and segment colors come from
    # the value mappings; showValue prints the state name on each segment instead.
    p["options"] = {"mergeValues": True, "showValue": "auto", "alignValue": "center",
                    "rowHeight": 0.9, "legend": {"showLegend": False},
                    "tooltip": {"mode": "single", "sort": "none"}}
    p["type"] = "state-timeline"
    strip(p, DISCRETE_KEYS)


def to_piechart(p):
    f = fc(p)
    lg = p.get("legend") or {}
    show = lg.get("show", True) if isinstance(lg, dict) else True
    p["options"] = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": True},
        "pieType": p.get("pieType", "pie"),
        "displayLabels": ["name", "percent"],
        "legend": {"displayMode": "list", "placement": "right", "showLegend": show,
                   "values": ["percent"]},
        "tooltip": {"mode": "single", "sort": "none"},
    }
    p["type"] = "piechart"
    strip(p, PIE_KEYS)


def to_geomap(p):
    p["options"] = {"basemap": {"type": "osm-standard"}, "view": {"id": "fit"},
                    "layers": [{"type": "markers",
                                "location": {"mode": "coords", "latitude": "lat", "longitude": "lng"},
                                "config": {"showLegend": False,
                                           "style": {"color": {"fixed": "#F2495C"},
                                                     "size": {"fixed": 6}, "opacity": 0.9}},
                                "tooltip": True}]}
    fc(p)
    p["type"] = "geomap"
    strip(p, MAP_KEYS)


def _num(v):
    try:
        return float(v) if "." in str(v) else int(v)
    except (ValueError, TypeError):
        return v


def needs_graph(p):
    return "yaxes" in p or "seriesOverrides" in p or (isinstance(p.get("legend"), dict)
                                                      and "alignAsTable" in (p.get("legend") or {}))


def fix_xychart(p):
    """Grafana 11.3+ rewrote xychart: old options used `seriesMapping`/`dims` and bare
    field-name strings (series[].x/y), which the new panel ignores -> empty plot. Rebuild
    into the new schema: mapping=manual, series[].{x,y,color} = {matcher: byName}."""
    o = p.get("options") or {}
    old = (o.get("series") or [{}])[0] if o.get("series") else {}
    dims = o.get("dims") or {}
    x = old.get("x") if isinstance(old.get("x"), str) else dims.get("x")
    y = old.get("y") if isinstance(old.get("y"), str) else None
    pc = old.get("pointColor") or {}
    colorf = pc.get("field") if isinstance(pc, dict) else None

    def m(field):
        return {"matcher": {"id": "byName", "options": field}}
    s = {}
    if x:
        s["x"] = m(x)
    if y:
        s["y"] = m(y)
    if colorf:
        s["color"] = m(colorf)
    p["options"] = {
        "mapping": "manual",
        "series": [s] if s else [],
        "legend": o.get("legend", {"showLegend": False, "displayMode": "list",
                                    "placement": "bottom", "calcs": []}),
        "tooltip": o.get("tooltip", {"mode": "single", "sort": "none"}),
    }


def convert(panels):
    n = 0
    for p in panels:
        t = p.get("type")
        opts = p.get("options") or {}
        dfl = (p.get("fieldConfig") or {}).get("defaults") or {}
        st_needs = ("valueMaps" in p) or ("perPage" in opts) \
            or (opts.get("legend", {}).get("showLegend")) \
            or (dfl.get("color", {}).get("mode") == "thresholds")
        if t == "natel-discrete-panel" or (t == "state-timeline" and st_needs):
            discrete_to_state_timeline(p); n += 1
        elif t in ("grafana-worldmap-panel", "pr0ps-trackmap-panel"):
            to_geomap(p); n += 1
        elif t == "grafana-piechart-panel":
            to_piechart(p); n += 1
        elif t in ("graph", "timeseries") and needs_graph(p):
            graph_to_timeseries(p); n += 1
        elif t == "graph":
            p["type"] = "timeseries"; n += 1
        elif t == "xychart" and ("seriesMapping" in opts or "dims" in opts):
            fix_xychart(p); n += 1
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
            print("  %s: fixed %d panel(s) -> %s" % (dash.get("title"), c, r.status_code))
        else:
            print("  %s: nothing to fix" % dash.get("title"))


if __name__ == "__main__":
    main()
