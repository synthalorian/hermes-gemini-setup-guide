"""
hermes_cli/runtime_provider.py — annotated diff sketch.

Apply this edit to the real file in your hermes tree.
"""

# =============================================================================
# 1. Add the import at the top
# =============================================================================

from hermes_cli.auth import (
    resolve_qwen_runtime_credentials,    # existing
    resolve_gemini_runtime_credentials,  # NEW
    # ... other resolvers ...
)


# =============================================================================
# 2. Add the gemini-oauth branch in resolve_runtime_credentials_for_model
# =============================================================================

def resolve_runtime_credentials_for_model(model_name, provider):
    """
    Walks the provider chain and returns runtime credentials.
    Add a branch for gemini-oauth alongside the existing oauth_external
    providers (qwen-oauth, openai-codex, etc.).
    """
    # ... existing branches for anthropic, openai, qwen-oauth, etc. ...

    if provider == "gemini-oauth":
        return resolve_gemini_runtime_credentials()

    # ... fall-through to api_key path or error ...
