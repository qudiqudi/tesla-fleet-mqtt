#!/usr/bin/env python3
"""
Repair cross-dashboard links after migration. grafana-migrate.py prefixes every dashboard
UID with 'tl-', but panel data links, panel links and dashboard links still point at the
old UIDs, so drilldowns (e.g. selecting a row in the Trip table) land on "Dashboard not
found". This rewrites every reference d/<old> and d-solo/<old> to d/tl-<old> when <old>
is a dashboard that lives in this folder. Other links (external URLs, the teslalogger admin
panel, dashboards outside the folder) are left untouched. Idempotent.

Run in the tools container:
  docker exec -e DST_GRAFANA_TOKEN=... tesla-tools python migration/grafana-fix-links.py
"""
import os
import re

from _grafana import folder_uid, for_each_dashboard, search_dashboards

DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
TOK = os.environ["DST_GRAFANA_TOKEN"]
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")


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
    fu = folder_uid(FOLDER, TOK, DST)
    if not fu:
        print("folder '%s' not found" % FOLDER); return
    items = search_dashboards(fu, TOK, DST)
    olds = [it["uid"][3:] for it in items if it["uid"].startswith("tl-")]
    if not olds:
        print("no tl- dashboards found"); return
    # match d/<old> or d-solo/<old> not already followed by more uid chars (so tl- ones skip)
    rx = re.compile(r"d/(" + "|".join(re.escape(u) for u in olds) + r")(?![A-Za-z0-9_-])")
    print("%d dashboards, %d link targets" % (len(items), len(olds)))
    for_each_dashboard(fu, lambda d: rewrite_strings(d, rx), TOK, DST,
                       "  %s: rewrote %d link(s) -> %s", items=items)


if __name__ == "__main__":
    main()
