#!/usr/bin/env bash
# verify-install.sh — sanity-check that hermes is wired to Google Code Assist
#
# Checks:
#   1. ~/.gemini/oauth_creds.json exists and has the expected fields
#   2. The `hermes` CLI binary is on PATH
#   3. `hermes auth list gemini-oauth` shows the credential as active
#   4. resolve_gemini_runtime_credentials() round-trips a refresh
#   5. A simple chat round-trips
#   6. A tool call round-trips (the real test — exercises thoughtSignature replay)

set -euo pipefail

# Colors
RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; CYAN=$'\e[36m'; RESET=$'\e[0m'

ok()    { echo "${GREEN}✓${RESET} $*"; }
fail()  { echo "${RED}✗${RESET} $*" >&2; exit 1; }
info()  { echo "${CYAN}ℹ${RESET} $*"; }
warn()  { echo "${YELLOW}!${RESET} $*"; }

CREDS="${HOME}/.gemini/oauth_creds.json"
HERMES_AGENT="${HERMES_AGENT:-${HOME}/.hermes/hermes-agent}"

# 1. Credentials file
info "Checking ${CREDS}..."
[[ -f "$CREDS" ]] || fail "Missing $CREDS — run \`gemini\` once to mint OAuth tokens."

PERMS="$(stat -c '%a' "$CREDS" 2>/dev/null || stat -f '%A' "$CREDS")"
[[ "$PERMS" == "600" ]] || warn "$CREDS perms are $PERMS (should be 600). Run: chmod 600 $CREDS"

if command -v jq >/dev/null 2>&1; then
    for field in access_token refresh_token expiry_date; do
        if jq -e "has(\"$field\")" "$CREDS" >/dev/null; then
            ok "credentials have field: $field"
        else
            fail "credentials missing field: $field"
        fi
    done
else
    warn "jq not installed — skipping credential field checks"
fi

# 2. hermes binary
info "Looking for the \`hermes\` binary..."
if command -v hermes >/dev/null 2>&1; then
    HERMES="$(command -v hermes)"
    ok "found hermes at $HERMES"
else
    fail "hermes binary not found. Install: cd ~/.hermes/hermes-agent && pip install -e ."
fi

# 3. Auth list
info "Running \`hermes auth list gemini-oauth\`..."
if "$HERMES" auth list gemini-oauth 2>&1 | tee /tmp/hermes-verify-1.txt | grep -qiE "(active|valid|gemini-oauth)"; then
    ok "gemini-oauth credential is registered"
else
    warn "gemini-oauth not found in auth pool — running \`hermes auth add gemini-oauth\`"
    "$HERMES" auth add gemini-oauth || fail "auth add failed"
fi

# 4. Live refresh round-trip
info "Running live OAuth refresh round-trip..."
if [[ -d "$HERMES_AGENT" && -f "$HERMES_AGENT/venv/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$HERMES_AGENT/venv/bin/activate"
    cd "$HERMES_AGENT"
    if python -c "
from hermes_cli.auth import resolve_gemini_runtime_credentials
creds = resolve_gemini_runtime_credentials(force_refresh=True)
print('access_token:', creds['api_key'][:24] + '...')
print('expires_at_ms:', creds['expires_at_ms'])
print('source:', creds['source'])
" 2>&1 | tee /tmp/hermes-verify-2.txt; then
        ok "live refresh round-trip succeeded"
    else
        fail "live refresh failed — see /tmp/hermes-verify-2.txt"
    fi
else
    warn "Couldn't find $HERMES_AGENT/venv — skipping live refresh test"
fi

# 5. Simple chat
info "Running simple chat..."
if "$HERMES" chat -q "say 'hello synthwave grid' in exactly four words" 2>&1 | tee /tmp/hermes-verify-3.txt | tail -3; then
    ok "simple chat round-tripped"
else
    fail "simple chat failed — see /tmp/hermes-verify-3.txt"
fi

# 6. Tool call round-trip (the real test)
info "Running tool call (the thoughtSignature test)..."
if "$HERMES" chat -q "use the memory tool to remember 'I like synthwave music' then briefly tell me what you remembered" \
    2>&1 | tee /tmp/hermes-verify-4.txt | tail -10; then
    if grep -qiE "(missing|thought_signature|400)" /tmp/hermes-verify-4.txt; then
        fail "looks like a thoughtSignature 400 — check the cache wiring in agent/google_codeassist_protocol.py"
    fi
    ok "tool call round-tripped — thoughtSignature replay is working"
else
    fail "tool call failed — see /tmp/hermes-verify-4.txt"
fi

echo
ok "All checks passed. hermes is wired to Google Code Assist correctly."
