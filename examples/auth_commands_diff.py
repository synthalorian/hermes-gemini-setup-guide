"""
hermes_cli/auth_commands.py — annotated diff sketch for the gemini-oauth provider.

Apply these edits to the real file in your hermes tree.
"""

import click

# Imports needed (add these to the top of the real file)
from hermes_cli.auth import (
    _read_gemini_cli_tokens,
    _gemini_cli_auth_path,
)


# =============================================================================
# 1. Add gemini-oauth to _OAUTH_CAPABLE_PROVIDERS
# =============================================================================

"""
_OAUTH_CAPABLE_PROVIDERS = {
    "qwen-oauth",
    "nous-portal",
    "openai-codex",
    "gemini-oauth",   # <-- NEW
}
"""


# =============================================================================
# 2. Add the auth_add_command branch (mirror Qwen's branch verbatim)
# =============================================================================

def auth_add_command_gemini_branch(auth_pool):
    """
    Inside auth_add_command(), after _normalize_provider, add this branch.
    Place it next to the qwen-oauth branch — they should look identical
    structurally.
    """
    normalized_provider = "gemini-oauth"  # set by _normalize_provider above

    if normalized_provider == "gemini-oauth":
        try:
            creds = _read_gemini_cli_tokens()
        except Exception as e:
            click.echo(f"Cannot adopt gemini credentials: {e}", err=True)
            return

        # Register in hermes's auth pool (same shape as qwen)
        auth_pool.add_credential(
            provider="gemini-oauth",
            credential=creds,
            source=str(_gemini_cli_auth_path()),
        )
        click.echo("✓ adopted gemini OAuth credentials from ~/.gemini/oauth_creds.json")
        return


# =============================================================================
# 3. Add aliases to _normalize_provider
# =============================================================================

# IMPORTANT: this alias map is SEPARATE from the one in auth.py!
# Both need to be updated when adding a new alias, or alias resolution
# will silently break in one of the dispatch paths.

"""
def _normalize_provider(name: str) -> str:
    ALIASES = {
        # ... existing entries ...

        "gemini-cli": "gemini-oauth",
        "google-oauth": "gemini-oauth",
        "google-gemini-cli": "gemini-oauth",
        "google-cli": "gemini-oauth",
    }
    return ALIASES.get(name.lower(), name.lower())
"""
