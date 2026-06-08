#!/usr/bin/env bash
# Exchange a Tesla authorization code for tokens, decode and show the granted scopes.
# Prints the refresh token to paste into .env. Prompts for the code only.
#
# First open this in a browser logged into your Tesla account, approve, and copy
# the ?code= value from the redirect URL (include &prompt_missing_scopes=true if you
# are adding a scope to an app the account already authorized):
#
#   https://auth.tesla.com/oauth2/v3/authorize?response_type=code&client_id=$TESLA_CLIENT_ID
#     &redirect_uri=<urlencoded TESLA_REDIRECT_URI>
#     &scope=openid%20offline_access%20vehicle_device_data%20vehicle_location%20vehicle_cmds%20vehicle_charging_cmds
#     &state=x&prompt_missing_scopes=true
set -uo pipefail
. "$(dirname "$0")/_env.sh"
need TESLA_CLIENT_ID TESLA_CLIENT_SECRET TESLA_REDIRECT_URI FLEET_API_BASE TESLA_AUTH_URL
command -v jq >/dev/null || { echo "jq is required"; exit 1; }

read -rp "Paste the authorization code (value after code= , before &state): " CODE
[ -z "$CODE" ] && { echo "no code given"; exit 1; }

RESP=$(curl -s "$TESLA_AUTH_URL" \
  --data-urlencode grant_type=authorization_code \
  --data-urlencode "client_id=$TESLA_CLIENT_ID" \
  --data-urlencode "client_secret=$TESLA_CLIENT_SECRET" \
  --data-urlencode "code=$CODE" \
  --data-urlencode "audience=$FLEET_API_BASE" \
  --data-urlencode "redirect_uri=$TESLA_REDIRECT_URI")

RT=$(echo "$RESP" | jq -r '.refresh_token // empty')
AT=$(echo "$RESP" | jq -r '.access_token // empty')
if [ -z "$RT" ]; then echo "FAILED:"; echo "$RESP" | jq . 2>/dev/null || echo "$RESP"; exit 1; fi

b64url_decode(){ local d="$1"; d="${d//-/+}"; d="${d//_//}"; local m=$(( ${#d} % 4 )); [ $m -eq 2 ] && d="$d=="; [ $m -eq 3 ] && d="$d="; printf '%s' "$d" | base64 -d 2>/dev/null; }
SCOPES=$(b64url_decode "$(echo "$AT" | cut -d. -f2)" | jq -r '(.scp // .scopes) | if type=="array" then join(" ") else (.//"") end' 2>/dev/null)

echo
echo "access token chars: ${#AT}"
echo "scopes in token: ${SCOPES:-could not decode}"
echo "$SCOPES" | grep -qw vehicle_location \
  && echo ">>> vehicle_location present (GPS will work)" \
  || echo ">>> vehicle_location MISSING — re-auth with vehicle_location in scope AND &prompt_missing_scopes=true"
echo
echo "Put this in .env as TESLA_REFRESH_TOKEN:"
echo "$RT"
