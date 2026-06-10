# Migrating from teslalogger

Optional helpers for people coming from [teslalogger](https://github.com/bassmaster187/TeslaLogger).
They are **not** part of the running integration — the core stack works without them. Use these
once if you want to bring your teslalogger history and dashboards over, then forget about them.

Nothing here is teslalogger code or dashboards: the Grafana scripts operate on *your own* Grafana
over its HTTP API at runtime, and the backfill reads from *your own* teslalogger database. No
GPL content is copied into this repo.

## What each script does

| Script | Purpose |
|--------|---------|
| `backfill-teslalogger.py` | One-time import of historical drives/charges from teslalogger's DB into this stack's DB. Idempotent (`source='backfill'`). |
| `grafana-migrate.py` | Copies every dashboard from your teslalogger Grafana into your target Grafana, repointing each panel at your target datasource. UIDs get a `tl-` prefix to avoid collisions. |
| `grafana-modernize.py` | Converts the dead Angular panel types (graph, natel-discrete, worldmap/trackmap, piechart plugin) to native React panels **and translates their display config** (axis units, legends, series overrides, value mappings) so labels/colors survive. Idempotent. |
| `grafana-fix-links.py` | Because migrate prefixes UIDs with `tl-`, cross-dashboard drilldown links still point at the old UIDs. Rewrites `d/<old>` → `d/tl-<old>` for in-folder targets. Idempotent. |
| `grafana-clean-maps.py` | teslalogger builds an HTML address tooltip the native geomap can't render; this strips the markup, hides the per-layer legend, and sets a high-contrast marker style. Idempotent. |

## Requirements

- Python 3 with `requests` (Grafana scripts) and `pymysql` (backfill).
- Network access to the source and target Grafana, and to both databases for the backfill.
- A Grafana service-account token with an editor/admin role on the **target** Grafana.

## Configuration (all via environment)

| Variable | Default | Used by |
|----------|---------|---------|
| `SRC_GRAFANA` | `http://teslalogger-grafana:3000` | migrate |
| `SRC_GRAFANA_TOKEN` | — | migrate |
| `DST_GRAFANA` | `http://grafana:3000` | all grafana scripts |
| `DST_GRAFANA_TOKEN` | — | all grafana scripts |
| `DST_DS_NAME` | `teslalogger` | migrate (target datasource name) |
| `DST_FOLDER` | `Tesla (teslalogger)` | all grafana scripts |
| `MARKER_COLOR` | `#F2495C` | clean-maps |
| `TL_DB_*`, `DB_*`, `TESLA_VIN` | see `backfill-teslalogger.py` | backfill |

Set `DST_GRAFANA` if your Grafana isn't reachable at `grafana:3000` (e.g. a custom port).

## Recommended order

```bash
# 1. history (run once)
python migration/backfill-teslalogger.py

# 2. dashboards: copy, then modernize the panels, fix drilldown links, clean the maps
python migration/grafana-migrate.py
python migration/grafana-modernize.py
python migration/grafana-fix-links.py
python migration/grafana-clean-maps.py
```

Every Grafana script is idempotent, so re-running is safe. Run them anywhere Python can reach
your Grafana — directly on a host, or inside a container that has the env vars and network access.
The Grafana scripts share `_grafana.py` (API calls with error checking, folder lookup, panel
walking), so keep it next to them.
