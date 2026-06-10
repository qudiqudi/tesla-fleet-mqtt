#!/usr/bin/env python3
"""
Copy all dashboards from one Grafana to another, repointing every panel at a target
datasource. Moves your own dashboards between your own instances via their HTTP APIs.

Tokens are passed in the environment (not stored). Run in the tools container:
  docker exec -e SRC_GRAFANA_TOKEN=... -e DST_GRAFANA_TOKEN=... \
    tesla-tools python migration/grafana-migrate.py
"""
import os

from _grafana import api, folder_uid, get_dashboard

SRC = os.environ.get("SRC_GRAFANA", "http://teslalogger-grafana:3000").rstrip("/")
DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
SRC_TOK = os.environ["SRC_GRAFANA_TOKEN"]
DST_TOK = os.environ["DST_GRAFANA_TOKEN"]
DST_DS_NAME = os.environ.get("DST_DS_NAME", "teslalogger")
DST_DS_TYPE = os.environ.get("DST_DS_TYPE", "mysql")
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")


def main():
    ds_uid = api("GET", "/api/datasources/name/%s" % DST_DS_NAME, DST_TOK, DST).json()["uid"]
    print("target datasource '%s' uid=%s" % (DST_DS_NAME, ds_uid))

    # folder to group the imported dashboards (create, or look up if it already exists)
    fr = api("POST", "/api/folders", DST_TOK, DST, payload={"title": FOLDER}, check=False)
    fu = fr.json().get("uid") if fr.status_code in (200, 201) else folder_uid(FOLDER, DST_TOK, DST)

    def repoint(o):
        if isinstance(o, dict):
            if "datasource" in o and o["datasource"] is not None:
                o["datasource"] = {"type": DST_DS_TYPE, "uid": ds_uid}
            for v in o.values():
                repoint(v)
        elif isinstance(o, list):
            for v in o:
                repoint(v)

    items = api("GET", "/api/search?type=dash-db", SRC_TOK, SRC).json()
    print("%d dashboards to migrate" % len(items))
    ok = 0
    for it in items:
        dash = get_dashboard(it["uid"], SRC_TOK, SRC)
        dash.pop("id", None)
        dash["uid"] = ("tl-" + (dash.get("uid") or it["uid"]))[:40]
        repoint(dash)
        payload = {"dashboard": dash, "folderUid": fu, "overwrite": True}
        pr = api("POST", "/api/dashboards/db", DST_TOK, DST, payload=payload, timeout=60, check=False)
        if pr.status_code == 200:
            ok += 1
            print("  ok: %s" % dash.get("title"))
        else:
            print("  FAIL: %s -> %s %s" % (dash.get("title"), pr.status_code, pr.text[:160]))
    print("migrated %d/%d into folder '%s'" % (ok, len(items), FOLDER))


if __name__ == "__main__":
    main()
