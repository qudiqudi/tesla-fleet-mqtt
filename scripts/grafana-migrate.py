#!/usr/bin/env python3
"""
Copy all dashboards from one Grafana to another, repointing every panel at a target
datasource. Moves your own dashboards between your own instances via their HTTP APIs.

Tokens are passed in the environment (not stored). Run in the tools container:
  docker exec -e SRC_GRAFANA_TOKEN=... -e DST_GRAFANA_TOKEN=... \
    tesla-tools python scripts/grafana-migrate.py
"""
import os
import requests

SRC = os.environ.get("SRC_GRAFANA", "http://teslalogger-grafana:3000").rstrip("/")
DST = os.environ.get("DST_GRAFANA", "http://grafana:3000").rstrip("/")
SRC_TOK = os.environ["SRC_GRAFANA_TOKEN"]
DST_TOK = os.environ["DST_GRAFANA_TOKEN"]
DST_DS_NAME = os.environ.get("DST_DS_NAME", "teslalogger")
DST_DS_TYPE = os.environ.get("DST_DS_TYPE", "mysql")
FOLDER = os.environ.get("DST_FOLDER", "Tesla (teslalogger)")

sh = {"Authorization": "Bearer " + SRC_TOK}
dh = {"Authorization": "Bearer " + DST_TOK, "Content-Type": "application/json"}


def main():
    r = requests.get("%s/api/datasources/name/%s" % (DST, DST_DS_NAME), headers=dh, timeout=30)
    r.raise_for_status()
    ds_uid = r.json()["uid"]
    print("target datasource '%s' uid=%s" % (DST_DS_NAME, ds_uid))

    # folder to group the imported dashboards
    folder_uid = None
    fr = requests.post("%s/api/folders" % DST, headers=dh, json={"title": FOLDER}, timeout=30)
    if fr.status_code in (200, 201):
        folder_uid = fr.json().get("uid")
    else:
        for f in requests.get("%s/api/folders" % DST, headers=dh, timeout=30).json():
            if f.get("title") == FOLDER:
                folder_uid = f.get("uid")

    def repoint(o):
        if isinstance(o, dict):
            if "datasource" in o and o["datasource"] is not None:
                o["datasource"] = {"type": DST_DS_TYPE, "uid": ds_uid}
            for v in o.values():
                repoint(v)
        elif isinstance(o, list):
            for v in o:
                repoint(v)

    items = requests.get("%s/api/search?type=dash-db" % SRC, headers=sh, timeout=30).json()
    print("%d dashboards to migrate" % len(items))
    ok = 0
    for it in items:
        full = requests.get("%s/api/dashboards/uid/%s" % (SRC, it["uid"]), headers=sh, timeout=30).json()
        dash = full["dashboard"]
        dash.pop("id", None)
        dash["uid"] = ("tl-" + (dash.get("uid") or it["uid"]))[:40]
        repoint(dash)
        payload = {"dashboard": dash, "folderUid": folder_uid, "overwrite": True}
        pr = requests.post("%s/api/dashboards/db" % DST, headers=dh, json=payload, timeout=60)
        if pr.status_code == 200:
            ok += 1
            print("  ok: %s" % dash.get("title"))
        else:
            print("  FAIL: %s -> %s %s" % (dash.get("title"), pr.status_code, pr.text[:160]))
    print("migrated %d/%d into folder '%s'" % (ok, len(items), FOLDER))


if __name__ == "__main__":
    main()
