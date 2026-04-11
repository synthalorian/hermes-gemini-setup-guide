"""
hermes_cli/auth.py — annotated diff sketch for the gemini-oauth provider.

This is NOT a runnable file. It shows the SHAPE of the changes needed in
hermes_cli/auth.py. Apply these edits to the real file in your hermes tree.
"""

# =============================================================================
# 1. Constants block (near the top, alongside QWEN_OAUTH_*)
# =============================================================================

# Embedded gemini-cli OAuth credentials. NOT secret — every gemini-cli install
# ships with these. Scoped to the Code Assist API surface only. The same pair
# is used by Google's official `gemini` CLI and by Nous Research's pi-ai library.
GEMINI_OAUTH_CLIENT_ID = "<GOOGLE_CLIENT_ID>"
GEMINI_OAUTH_CLIENT_SECRET = "<GOOGLE_CLIENT_SECRET>"
GEMINI_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh access tokens this many seconds before expiry
GEMINI_OAUTH_REFRESH_SKEW_SECONDS = 120


# =============================================================================
# 2. PROVIDER_REGISTRY entry (alongside the qwen-oauth entry)
# =============================================================================

# Inside the dict literal:
"""
PROVIDER_REGISTRY = {
    # ... existing entries ...

    "gemini-oauth": ProviderConfig(
        id="gemini-oauth",
        display_name="Google Gemini (OAuth)",
        auth_type="oauth_external",
        client_id=GEMINI_OAUTH_CLIENT_ID,
        token_url=GEMINI_OAUTH_TOKEN_URL,
        api_key_env_vars=(),  # OAuth, not API key
        cli_tool="gemini",
        cli_creds_path="~/.gemini/oauth_creds.json",
    ),
}
"""


# =============================================================================
# 3. Aliases (extend _PROVIDER_ALIASES)
# =============================================================================

"""
_PROVIDER_ALIASES = {
    # ... existing entries ...

    "gemini-oauth": "gemini-oauth",     # canonical
    "gemini-cli": "gemini-oauth",
    "google-oauth": "gemini-oauth",
    "google-gemini-cli": "gemini-oauth",
    "google-cli": "gemini-oauth",
}
"""


# =============================================================================
# 4. The 7 helper functions (mirror Qwen — copy + rename)
# =============================================================================

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

import requests


def _gemini_cli_auth_path() -> Path:
    """Path to ~/.gemini/oauth_creds.json — the file Google's gemini CLI writes."""
    return Path.home() / ".gemini" / "oauth_creds.json"


def _read_gemini_cli_tokens() -> Dict[str, Any]:
    """Read tokens from ~/.gemini/oauth_creds.json. Raises AuthError on failure."""
    path = _gemini_cli_auth_path()
    if not path.exists():
        raise AuthError(  # type: ignore[name-defined]
            provider="gemini-oauth",
            message=f"missing {path} — run `gemini` to mint tokens first",
        )
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise AuthError(  # type: ignore[name-defined]
            provider="gemini-oauth",
            message=f"invalid JSON in {path}: {e}",
        )


def _save_gemini_cli_tokens(tokens: Dict[str, Any]) -> None:
    """Write tokens back to ~/.gemini/oauth_creds.json with chmod 600."""
    path = _gemini_cli_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(path, 0o600)


def _gemini_access_token_is_expiring(tokens: Dict[str, Any]) -> bool:
    """True if the access_token is within REFRESH_SKEW_SECONDS of expiry.

    NOTE: Google's oauth_creds.json uses `expiry_date` in MILLISECONDS,
    not `expires_at` in seconds like Qwen. If you're copying from the qwen
    helper, convert units.
    """
    expiry_ms = tokens.get("expiry_date", 0)
    now_ms = int(time.time() * 1000)
    return (expiry_ms - now_ms) < (GEMINI_OAUTH_REFRESH_SKEW_SECONDS * 1000)


def _refresh_gemini_cli_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    """POST to oauth2.googleapis.com/token to mint a fresh access_token."""
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise AuthError(  # type: ignore[name-defined]
            provider="gemini-oauth",
            message="no refresh_token in oauth_creds.json — re-run `gemini`",
        )

    response = requests.post(
        GEMINI_OAUTH_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": GEMINI_OAUTH_CLIENT_ID,
            "client_secret": GEMINI_OAUTH_CLIENT_SECRET,
        },
        timeout=30,
    )
    if response.status_code != 200:
        raise AuthError(  # type: ignore[name-defined]
            provider="gemini-oauth",
            message=f"refresh failed: {response.status_code} {response.text}",
        )

    body = response.json()
    new_tokens = dict(tokens)  # Preserve fields like project_id, refresh_token, etc.
    new_tokens["access_token"] = body["access_token"]
    new_tokens["expiry_date"] = int(time.time() * 1000) + body.get("expires_in", 3599) * 1000
    return new_tokens


def resolve_gemini_runtime_credentials(force_refresh: bool = False) -> Dict[str, Any]:
    """Top-level entry point — used by runtime_provider.py.

    Returns a dict with the same shape as other oauth_external providers:
        provider, api_mode, base_url, api_key, source, expires_at_ms,
        requested_provider
    """
    tokens = _read_gemini_cli_tokens()
    if force_refresh or _gemini_access_token_is_expiring(tokens):
        tokens = _refresh_gemini_cli_tokens(tokens)
        _save_gemini_cli_tokens(tokens)
    return {
        "provider": "gemini-oauth",
        "api_mode": "chat_completions",
        "base_url": "https://cloudcode-pa.googleapis.com",
        "api_key": tokens["access_token"],
        "source": str(_gemini_cli_auth_path()),
        "expires_at_ms": tokens.get("expiry_date"),
        "requested_provider": "gemini-oauth",
    }


def get_gemini_auth_status() -> Dict[str, Any]:
    """Used by `hermes auth list` to display credential health."""
    try:
        tokens = _read_gemini_cli_tokens()
        return {
            "active": True,
            "expires_at_ms": tokens.get("expiry_date"),
            "source": str(_gemini_cli_auth_path()),
        }
    except Exception as e:  # AuthError or anything else
        return {"active": False, "error": str(e)}
