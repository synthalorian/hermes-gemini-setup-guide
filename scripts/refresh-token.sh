#!/usr/bin/env bash
# refresh-token.sh — manually refresh ~/.gemini/oauth_creds.json
#
# Useful for debugging when hermes fails with 401 and you want to confirm
# the refresh token still works without going through hermes itself.

set -euo pipefail

CREDS="${HOME}/.gemini/oauth_creds.json"

# Embedded gemini-cli OAuth credentials (NOT secret — every install ships these)
CLIENT_ID="<GOOGLE_CLIENT_ID>"
CLIENT_SECRET="<GOOGLE_CLIENT_SECRET>"
TOKEN_URL="https://oauth2.googleapis.com/token"

[[ -f "$CREDS" ]] || { echo "Missing $CREDS — run \`gemini\` once first." >&2; exit 1; }
command -v jq >/dev/null  || { echo "Need jq installed." >&2; exit 1; }
command -v curl >/dev/null || { echo "Need curl installed." >&2; exit 1; }

REFRESH_TOKEN="$(jq -r '.refresh_token' "$CREDS")"
[[ -n "$REFRESH_TOKEN" && "$REFRESH_TOKEN" != "null" ]] || { echo "No refresh_token in $CREDS." >&2; exit 1; }

echo "Refreshing access token..."

RESPONSE="$(curl -sS -X POST "$TOKEN_URL" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    --data-urlencode "grant_type=refresh_token" \
    --data-urlencode "refresh_token=${REFRESH_TOKEN}" \
    --data-urlencode "client_id=${CLIENT_ID}" \
    --data-urlencode "client_secret=${CLIENT_SECRET}")"

if echo "$RESPONSE" | jq -e '.error' >/dev/null 2>&1; then
    echo "Refresh failed:" >&2
    echo "$RESPONSE" | jq . >&2
    exit 1
fi

NEW_TOKEN="$(echo "$RESPONSE" | jq -r '.access_token')"
EXPIRES_IN="$(echo "$RESPONSE" | jq -r '.expires_in')"
NOW_MS=$(($(date +%s) * 1000))
EXPIRY_MS=$((NOW_MS + EXPIRES_IN * 1000))

# Write the refreshed values back, preserving everything else
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
jq --arg at "$NEW_TOKEN" --argjson ed "$EXPIRY_MS" \
    '.access_token = $at | .expiry_date = $ed' \
    "$CREDS" > "$TMP"
mv "$TMP" "$CREDS"
chmod 600 "$CREDS"

echo "✓ refreshed. New access_token starts with: ${NEW_TOKEN:0:24}..."
echo "✓ expires in $EXPIRES_IN seconds (epoch_ms = $EXPIRY_MS)"
