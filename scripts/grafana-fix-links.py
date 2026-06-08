#!/usr/bin/env python3
"""
Repair cross-dashboard links after migration. grafana-migrate.py prefixes every dashboard
UID with 'tl-', but panel data links, panel links and dashboard links still point at the
old UIDs, so drilldowns (e.g. selecting a row in the Trip table) land on "Dashboard not
found". This rewrites every reference d/<old> and d-solo/<old> to d/tl-<old> when <old>
is a dashboard that lives in this folder. Other links (external URLs, the teslalogger admin
panel, dashboards outside the folder) are left untouched. Idempotent.

Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python scripts/grafana-fix-links.py
"""
import os
import re
import requests

DST = os.environ.get("DST_GRAFANA", "http://grafana:3003").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")
h = {"Authorization": "Bearer " + TOK, "Content-Type": "application/json"}


def rewrite_strings(o, rx):
    """Recursively apply rx.sub over every string in the structure, in place."""
    n = 0
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str):
                nv = rx.sub(r"d/tl-\1", v)
                if nv != v:
                    o[k] = nv; n += 1
            else:
                n += rewrite_strings(v, rx)
    elif isinstance(o, list):
        for i, v in enumerate(o):
            if isinstance(v, str):
                nv = rx.sub(r"d/tl-\1", v)
                if nv != v:
                    o[i] = nv; n += 1
            else:
                n += rewrite_strings(v, rx)
    return n


def main():
    folder_uid = None
    for f in requests.get("%s/api/folders" % DST, headers=h, timeout=30).json():
        if f.get("title") == FOLDER:
            folder_uid = f.get("uid")
    if not folder_uid:
        print("folder '%s' not found" % FOLDER); return
    items = requests.get("%s/api/search?type=dash-db&folderUIDs=%s" % (DST, folder_uid),
                         headers=h, timeout=30).json()
    olds = [it["uid"][3:] for it in items if it["uid"].startswith("tl-")]
    if not olds:
        print("no tl- dashboards found"); return
    # match d/<old> or d-solo/<old> not already followed by more uid chars (so tl- ones skip)
    rx = re.compile(r"d/(" + "|".join(re.escape(u) for u in olds) + r")(?![A-Za-z0-9_-])")
    print("%d dashboards, %d link targets" % (len(items), len(olds)))
    for it in items:
        full = requests.get("%s/api/dashboards/uid/%s" % (DST, it["uid"]), headers=h, timeout=30).json()
        dash = full["dashboard"]
        c = rewrite_strings(dash, rx)
        if c:
            r = requests.post("%s/api/dashboards/db" % DST, headers=h, timeout=60,
                              json={"dashboard": dash, "overwrite": True, "folderUid": folder_uid})
            print("  %s: rewrote %d link(s) -> %s" % (dash.get("title"), c, r.status_code))


if __name__ == "__main__":
    main()
