#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# refresh-turn.sh  —  refresh Cloudflare TURN credentials in go2rtc.yaml
#
# Cloudflare TURN credentials are short-lived (~24h), and go2rtc has no way to
# fetch them itself, so this script regenerates them and restarts go2rtc.
# Run it from cron, e.g. every 12h:
#
#   0 */12 * * *  TURN_KEY_ID=xxx TURN_KEY_API_TOKEN=yyy /home/raspberry4/refresh-turn.sh >> /var/log/turn-refresh.log 2>&1
#
# SECURITY: never hardcode the secrets in a committed file. Pass them via the
# environment (cron line above) or a chmod-600 local env file you `source`.
# This script itself contains NO secrets and is safe to commit.
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

: "${TURN_KEY_ID:?set TURN_KEY_ID}"
: "${TURN_KEY_API_TOKEN:?set TURN_KEY_API_TOKEN}"
GO2RTC_YAML="${GO2RTC_YAML:-/home/raspberry4/go2rtc/go2rtc.yaml}"
TTL="${TTL:-86400}"                       # 24h (longer values are rejected)
RESTART_CMD="${RESTART_CMD:-sudo systemctl restart go2rtc}"

resp=$(curl -fsS \
  "https://rtc.live.cloudflare.com/v1/turn/keys/${TURN_KEY_ID}/credentials/generate-ice-servers" \
  -H "Authorization: Bearer ${TURN_KEY_API_TOKEN}" \
  -H "Content-Type: application/json" \
  --data "{\"ttl\": ${TTL}}")

user=$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin)["iceServers"][1]["username"])')
cred=$(printf '%s' "$resp" | python3 -c 'import sys,json; print(json.load(sys.stdin)["iceServers"][1]["credential"])')

if [ -z "$user" ] || [ -z "$cred" ]; then
  echo "ERROR: could not parse credentials from API response" >&2
  exit 1
fi

# Replace the first username:/credential: lines under webrtc.ice_servers.
sed -i -E "0,/^([[:space:]]*username:).*/s//\1 ${user}/" "$GO2RTC_YAML"
sed -i -E "0,/^([[:space:]]*credential:).*/s//\1 ${cred}/" "$GO2RTC_YAML"

eval "$RESTART_CMD" || pkill -f go2rtc || true
echo "TURN creds refreshed $(date -Is)"
