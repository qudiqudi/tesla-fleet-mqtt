#!/usr/bin/env python3
"""
Repair cross-dashboard links after migration. grafana-migrate.py prefixes every dashboard
UID with 'tl-', but panel data links, panel links and dashboard links still point at the
old UIDs, so drilldowns (e.g. selecting a row in the Trip table) land on "Dashboard not
found". This rewrites every reference d/<old> and d-solo/<old> to d/tl-<old> when <old>
is a dashboard that lives in this folder. Dashboards outside the folder are left untouched.

It also severs the two links that depend on teslalogger's web admin, so the dashboards keep
working once teslalogger is sunset: the address-column "Add Geofence" data link (a redirect to
teslalogger's geoadd.php) is repointed to OpenStreetMap at the row's coordinates, and the dead
"Admin Panel" nav links (to teslalogger's /admin/ home) are removed. Idempotent.

Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python migration/grafana-fix-links.py
"""
import os
import re

from _grafana import folder_uid, for_each_dashboard, search_dashboards

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")

# teslalogger's "Add Geofence" data link redirects to .../admin/geoadd.php?lat=..&lng=..; with
# teslalogger gone there's nowhere to add a geofence (home is configured via tlwriter's HOME_* env),
# so repoint it to a map of the spot instead. Keep the lat/lng Grafana field expressions verbatim.
GEOADD = re.compile(r"https?://[^\s\"']*?/geoadd\.php\?lat=([^&\"'\s]+)&lng=([^&\"'\s]+)")
# bare teslalogger admin home (the "Admin Panel" nav link) -- dead post-sunset, drop it. Does not
# match geoadd.php (that ends in a filename, not /admin/).
ADMIN = re.compile(r"https?://[^\s\"']*/admin/?$")


def _osm(m):
    lat, lng = m.group(1), m.group(2)
    return "https://www.openstreetmap.org/?mlat=%s&mlon=%s#map=17/%s/%s" % (lat, lng, lat, lng)


def rewrite_strings(o, fn):
    """Recursively apply fn (str -> str) over every string in the structure, in place."""
    n = 0
    if isinstance(o, dict):
        items = o.items()
    elif isinstance(o, list):
        items = enumerate(o)
    else:
        return 0
    for k, v in items:
        if isinstance(v, str):
            nv = fn(v)
            if nv != v:
                o[k] = nv; n += 1
        else:
            n += rewrite_strings(v, fn)
    return n


def drop_admin_links(o):
    """Remove link entries whose url is teslalogger's admin home from any `links` array
    (dashboard nav links and panel links). Data links live under `value`, not `links`, so
    the geoadd ones are untouched here -- they're repointed to OSM by rewrite_strings."""
    n = 0
    if isinstance(o, dict):
        for k, v in list(o.items()):
            if k == "links" and isinstance(v, list):
                keep = [ln for ln in v if not (isinstance(ln, dict)
                        and isinstance(ln.get("url"), str) and ADMIN.match(ln["url"]))]
                if len(keep) != len(v):
                    o[k] = keep; n += len(v) - len(keep)
            else:
                n += drop_admin_links(v)
    elif isinstance(o, list):
        for v in o:
            n += drop_admin_links(v)
    return n


def main():
    fu = folder_uid(FOLDER, TOK, DST)
    if not fu:
        print("folder '%s' not found" % FOLDER); return
    items = search_dashboards(fu, TOK, DST)
    olds = [it["uid"][3:] for it in items if it["uid"].startswith("tl-")]
    if not olds:
        print("no tl- dashboards found"); return
    # match d/<old> or d-solo/<old> not already followed by more uid chars (so tl- ones skip)
    rx = re.compile(r"d/(" + "|".join(re.escape(u) for u in olds) + r")(?![A-Za-z0-9_-])")

    def fix_string(s):
        s = rx.sub(r"d/tl-\1", s)              # cross-dashboard drilldowns -> tl- uids
        s = GEOADD.sub(_osm, s)                # "Add Geofence" redirect -> OpenStreetMap
        if s.strip() == "Add Geofence":        # and its now-misleading link title
            s = "Map"
        return s

    def fix(d):
        return rewrite_strings(d, fix_string) + drop_admin_links(d)

    print("%d dashboards, %d link targets" % (len(items), len(olds)))
    for_each_dashboard(fu, fix, TOK, DST, "  %s: changed %d link(s) -> %s", items=items)


if __name__ == "__main__":
    main()
