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
| `grafana-fix-links.py` | Because migrate prefixes UIDs with `tl-`, cross-dashboard drilldown links still point at the old UIDs. Rewrites `d/<old>` → `d/tl-<old>` for in-folder targets. Also severs the teslalogger-admin dependencies so dashboards survive its sunset: the address-column "Add Geofence" link is repointed to OpenStreetMap at the row's coordinates, and the dead "Admin Panel" nav links are removed. Idempotent. |
| `cleanup-geofences.py` | One-time normalisation of teslalogger geofence labels left in `pos.address`. Home positions (within `HOME_RADIUS` of `HOME_LAT`/`HOME_LNG`) → `HOME_LABEL`; every other geofence label (work, named chargers) is reverse-geocoded to a street address, one Nominatim request per distinct location. Idempotent. |
| `name-chargers.py` | One-time backfill of charger names onto past charge stops (the live counterpart runs in tlwriter at charge end). Each charge location is named after its operator via Open Charge Map (needs `OCM_API_KEY`) then OSM Overpass; unknown spots keep their address, home stays `HOME_LABEL`. Co-located chargers are disambiguated by the charge's recorded `fast_charger_brand` (Tesla vs third-party CCS); brand-less charges inherit the brand of the nearest recorded charge within `CHARGER_RADIUS` (spatial, so it isn't fooled by rounding) — so it also **corrects** stops the first pass mislabelled. A final pass propagates each charger name onto the drive start/end positions that sit on it, so trips from/to a charger show the operator (like teslalogger's geofence). One lookup per location, idempotent. |
| `grafana-fix-datasource-tz.py` | Sets the MySQL datasource session timezone so Grafana reads the DB's wall-clock timestamps correctly. The teslalogger schema (and tlwriter, matching it) stores **local** time, so this sets `SYSTEM` (the MariaDB container's `Europe/Berlin`, DST-aware). Idempotent. |
| `fix-timezone.py` | One-time: after switching tlwriter to local-time storage, shifts the rows tlwriter had written in UTC into local time (+2h, CEST). Touches only rows past the snapshot fork (the highest id this DB still shares with the source teslalogger DB); the imported local history is left alone. Dry-run by default (`CONFIRM=yes` to apply), marker-guarded against a second apply. |
| `grafana-clean-maps.py` | teslalogger builds an HTML address tooltip the native geomap can't render; this strips the markup, hides the per-layer legend, sets a high-contrast marker style, converts ordered position-history maps to thin route layers, adds parked/charging landmark layers, and rebuilds the "Visited" map (track + chargers in one UNION) into a teslalogger-style route line plus charger pins. Idempotent. |

## Requirements

- Python 3 with `requests` (Grafana scripts) and `pymysql` (backfill).
- Network access to the source and target Grafana, to both databases for the backfill, and to
  the Nominatim endpoint for cleanup-geofences.
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
| `VISITED_MARKER_SIZE` | `3` | clean-maps |
| `ROUTE_COLOR` | `#E02F44` | clean-maps |
| `ROUTE_WIDTH` | `2` | clean-maps |
| `MAP_FIT_LAYER` | `Position history` | clean-maps route maps only |
| `MAP_FIT_PADDING` | `8` | clean-maps |
| `MAP_FIT_MAX_ZOOM` | `14` | clean-maps |
| `PARK_COLOR` | `#3274D9` | clean-maps |
| `AC_CHARGE_COLOR` | `#FFD400` | clean-maps |
| `DC_CHARGE_COLOR` | `#E02F44` | clean-maps |
| `PARK_LABEL_COLOR` | `#FFFFFF` | clean-maps |
| `VISITED_ROUTE_COLOR` | `#2D7BFF` | clean-maps Visited track line |
| `VISITED_ROUTE_WIDTH` | `3` | clean-maps Visited track line |
| `VISITED_SC_COLOR` | `#E02F44` | clean-maps Supercharger disc (red) |
| `VISITED_DC_COLOR` | `#56A64B` | clean-maps other fast-charger disc (green) |
| `VISITED_HALO_COLOR` | `#FFFFFF` | clean-maps charger pin border |
| `VISITED_GLYPH_COLOR` | `#FFFFFF` | clean-maps charger glyph |
| `VISITED_SC_GLYPH` | `T` | clean-maps Supercharger glyph |
| `VISITED_DC_GLYPH` | `⚡` | clean-maps other fast-charger glyph |
| `VISITED_DOT_SIZE` | `8` | clean-maps charger disc radius |
| `VISITED_HALO_WIDTH` | `3` | clean-maps pin border width |
| `TL_DB_*`, `DB_*`, `TESLA_VIN` | see `backfill-teslalogger.py` | backfill |
| `DB_*`, `TLW_DB_NAME`, `TESLA_VIN` | see tlwriter | cleanup-geofences |
| `HOME_LAT`/`HOME_LNG`, `HOME_RADIUS`, `HOME_LABEL` | unset / `50` / `Home` | cleanup-geofences (home zone, same as tlwriter) |
| `NOMINATIM_URL`, `GEOCODE_USER_AGENT`, `GEOCODE_MIN_INTERVAL` | OSM / tlwriter UA / `1.1` | cleanup-geofences (reverse geocoding) |
| `DB_*`, `TLW_DB_NAME`, `TESLA_VIN`, `HOME_*` | see tlwriter | name-chargers |
| `OCM_API_KEY`, `OCM_API_URL`, `OVERPASS_URL`, `CHARGER_RADIUS` | — / OCM / Overpass / `75` | name-chargers (charger lookup) |

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
