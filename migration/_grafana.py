#!/usr/bin/env python3
"""
Shared Grafana HTTP helpers for the migration scripts: bearer-token requests with
error checking, folder lookup, the recursive panel walk, and the fetch-transform-post
loop over every dashboard in a folder.
"""
import sys

import requests


def api(method, path, token, base_url, payload=None, timeout=30, check=True):
    """One Grafana API call. Exits with the error body on non-2xx unless check=False."""
    r = requests.request(method, base_url + path,
                         headers={"Authorization": "Bearer " + token,
                                  "Content-Type": "application/json"},
                         json=payload, timeout=timeout)
    if check and not r.ok:
        print("%s %s -> HTTP %d: %s" % (method, path, r.status_code, r.text[:200]))
        sys.exit(1)
    return r


def folder_uid(title, token, base_url):
    """UID of the folder with this title, or None."""
    for f in api("GET", "/api/folders", token, base_url).json():
        if f.get("title") == title:
            return f.get("uid")
    return None


def search_dashboards(fuid, token, base_url):
    return api("GET", "/api/search?type=dash-db&folderUIDs=%s" % fuid, token, base_url).json()


def get_dashboard(uid, token, base_url):
    return api("GET", "/api/dashboards/uid/%s" % uid, token, base_url).json()["dashboard"]


def post_dashboard(dash, fuid, token, base_url, check=True):
    return api("POST", "/api/dashboards/db", token, base_url, timeout=60, check=check,
               payload={"dashboard": dash, "overwrite": True, "folderUid": fuid})


def walk_panels(panels, visit):
    """Apply visit to every panel, descending into nested 'panels'; sums the returns."""
    n = 0
    for p in panels:
        n += visit(p) or 0
        if "panels" in p:
            n += walk_panels(p["panels"], visit)
    return n


def for_each_dashboard(fuid, transform, token, base_url, msg, unchanged_msg=None, items=None):
    """Fetch each dashboard in the folder, apply transform (returns change count), and
    POST changed ones back with overwrite. msg gets (title, count, status_code)."""
    if items is None:
        items = search_dashboards(fuid, token, base_url)
    for it in items:
        dash = get_dashboard(it["uid"], token, base_url)
        c = transform(dash)
        if c:
            r = post_dashboard(dash, fuid, token, base_url)
            print(msg % (dash.get("title"), c, r.status_code))
        elif unchanged_msg:
            print(unchanged_msg % dash.get("title"))
