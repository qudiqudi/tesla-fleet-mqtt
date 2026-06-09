#!/usr/bin/env bash
# Print the Tesla authorize URL. Open it in a browser logged into your Tesla account,
# approve, copy the code= value (before &state), then run get-token.sh and paste it.
# Used for first-time setup and for re-auth when the refresh-token lineage dies.
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need TESLA_CLIENT_ID TESLA_REDIRECT_URI
SCOPE="openid offline_access vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds"
enc() { python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"; }
echo "https://auth.tesla.com/oauth2/v3/authorize?response_type=code&client_id=${TESLA_CLIENT_ID}&redirect_uri=$(enc "$TESLA_REDIRECT_URI")&scope=$(enc "$SCOPE")&state=x&prompt_missing_scopes=true"
