# Pattern Mirror: gemini-oauth ↔ qwen-oauth

The `gemini-oauth` provider is **intentionally a near-line-for-line mirror of the `qwen-oauth` provider**. This document gives the side-by-side mapping so future devs can compare them when debugging or extending either one.

## Why mirror Qwen?

Qwen was the first `oauth_external` provider hermes shipped — the pattern was already proven in production by the time gemini-oauth landed. Mirroring it line-for-line gives you:

- **Free regression coverage** — if you break gemini, the same break would break qwen, and the qwen tests will catch it
- **Predictable code review** — reviewers familiar with qwen can read gemini at a glance
- **Easy backporting** — bug fixes flow in both directions

## File-by-file mapping

| Qwen file/symbol | Gemini equivalent | Notes |
|---|---|---|
| `~/.qwen/oauth_creds.json` | `~/.gemini/oauth_creds.json` | Same shape, different keys (Google adds `expiry_date` in ms, Qwen uses `expires_at` in seconds — be careful here) |
| `QWEN_OAUTH_CLIENT_ID` | `GEMINI_OAUTH_CLIENT_ID` | Embedded constants in `auth.py` |
| `QWEN_OAUTH_CLIENT_SECRET` | `GEMINI_OAUTH_CLIENT_SECRET` | Both NOT secret — every CLI install ships them |
| `QWEN_OAUTH_TOKEN_URL` | `GEMINI_OAUTH_TOKEN_URL` | `chat.qwen.ai/.../token` vs `oauth2.googleapis.com/token` |
| `_qwen_cli_auth_path()` | `_gemini_cli_auth_path()` | Returns `Path` to creds file |
| `_read_qwen_cli_tokens()` | `_read_gemini_cli_tokens()` | Reads + json-decodes creds file |
| `_save_qwen_cli_tokens()` | `_save_gemini_cli_tokens()` | Writes back with chmod 600 |
| `_qwen_access_token_is_expiring()` | `_gemini_access_token_is_expiring()` | Compares expiry timestamp to now+skew |
| `_refresh_qwen_cli_tokens()` | `_refresh_gemini_cli_tokens()` | POST to TOKEN_URL with refresh_token grant |
| `resolve_qwen_runtime_credentials()` | `resolve_gemini_runtime_credentials()` | Top-level entry point used by `runtime_provider.py` |
| `get_qwen_auth_status()` | `get_gemini_auth_status()` | Used by `hermes auth list` |
| `tests/hermes_cli/test_auth_qwen_provider.py` | `tests/hermes_cli/test_auth_gemini_provider.py` | 36 tests, line-for-line mirror |

## Differences (where the mirror breaks)

The mirror is intentional, but **three places** had to diverge from Qwen:

### 1. Expiry timestamp shape

- Qwen `oauth_creds.json` uses `expires_at` (seconds since epoch)
- Google `oauth_creds.json` uses `expiry_date` (milliseconds since epoch)

So `_gemini_access_token_is_expiring()` does:

```python
expiry_ms = tokens.get("expiry_date", 0)
now_ms = int(time.time() * 1000)
return (expiry_ms - now_ms) < (GEMINI_OAUTH_REFRESH_SKEW_SECONDS * 1000)
```

…where the Qwen equivalent does:

```python
expires_at = tokens.get("expires_at", 0)
now = time.time()
return (expires_at - now) < QWEN_OAUTH_REFRESH_SKEW_SECONDS
```

If you ever copy code between them, **convert units**.

### 2. The need for a custom client

Qwen exposes a clean OpenAI-compat endpoint, so hermes can use a vanilla `openai.OpenAI(base_url="https://chat.qwen.ai/...")` client and call it a day.

Gemini's Cloud Code Assist API is **not** OpenAI-compat. It uses Google's content-parts shape with `thoughtSignature`, project discovery, and the LRO onboarding flow. So gemini-oauth needs a custom drop-in client (`GoogleCodeAssistClient`) that exposes an `openai.OpenAI`-shaped interface but talks to Code Assist underneath.

The branch in `run_agent.py::_create_openai_client` that swaps the client per provider is where this divergence shows up:

```python
def _create_openai_client(self):
    if self.provider == "gemini-oauth":
        from agent.google_codeassist_client import GoogleCodeAssistClient
        return GoogleCodeAssistClient(api_key=self.creds["api_key"])
    # Qwen and other oauth_external providers use plain openai.OpenAI:
    return openai.OpenAI(base_url=self.base_url, api_key=self.api_key)
```

### 3. The thoughtSignature side channel

Qwen has no thinking-mode replay requirement. Gemini does. The `_SIGNATURE_CACHE` module-level dict in `google_codeassist_protocol.py` is unique to gemini-oauth. See [`thought-signature.md`](thought-signature.md) for the deep dive.

## Test parity

The Gemini test file (`tests/hermes_cli/test_auth_gemini_provider.py`) has the same shape as the Qwen test file (`tests/hermes_cli/test_auth_qwen_provider.py`). The numbers from when this was first landed:

- `test_auth_qwen_provider.py`: ~33 tests
- `test_auth_gemini_provider.py`: 36 tests (3 extras for the expiry-unit conversion)

Run them side by side when changing either:

```bash
python -m pytest tests/hermes_cli/test_auth_qwen_provider.py tests/hermes_cli/test_auth_gemini_provider.py -v
```

## Recipe: adding ANOTHER `oauth_external` provider

If you ever need a third one (e.g. some hypothetical `claude-cli-oauth`), the mirror pattern means you have a clear template:

1. **`auth.py`** — add constants block (TOKEN_URL, CLIENT_ID, refresh skew), `PROVIDER_REGISTRY` entry, the 7 helper functions, aliases in `_PROVIDER_ALIASES`
2. **`auth_commands.py`** — add provider id to `_OAUTH_CAPABLE_PROVIDERS`, branch in `auth_add_command` (copy the gemini branch), aliases in `_normalize_provider`
3. **`runtime_provider.py`** — import `resolve_<provider>_runtime_credentials`, add a branch
4. **`main.py`** — add to `provider_labels`, `top_providers` (or `extended_providers`), the dispatch chain, write a `_model_flow_<provider>` function
5. **`agent/model_metadata.py`** — add provider id to `_PROVIDER_PREFIXES`
6. **`agent/models_dev.py`** — add provider id → models.dev provider name mapping
7. **`tests/hermes_cli/test_auth_<provider>_provider.py`** — copy `test_auth_gemini_provider.py` and rename
8. (Only if the API isn't OpenAI-compat) Add a custom client wrapper under `agent/`

The Gemini implementation is the cleanest "do exactly this" reference because it landed after the pattern was already proven by Qwen and Codex.
