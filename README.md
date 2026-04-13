# Hermes × Google Gemini (Code Assist) Setup Guide

A step-by-step recipe for adding Google Gemini OAuth support to [`hermes-agent`](https://github.com/nousresearch/hermes-agent) using the same OAuth tokens that Google's official `gemini` CLI mints. Hermes adopts the credentials in place — no parallel token store, no API key required, and you get the **Cloud Code Assist free tier** plus the **`thoughtSignature` replay** fix needed for Gemini 3 thinking-mode multi-turn tool calling.

> This is a guide repo, not a fork. It documents how to add `gemini-oauth` to your hermes install — patterned after the existing `qwen-oauth` provider so you can compare them line-for-line.

---

## Why this path?

Google exposes Gemini through three different surfaces. Picking the right one matters for an agent gateway that does tool calling:

| Surface | Endpoint | Auth | Cost | Tool calling |
|---|---|---|---|---|
| Generative Language (OpenAI-compat) | `generativelanguage.googleapis.com/v1beta/openai` | API key | Paid / free quota | Breaks on Gemini 3 (no thoughtSignature support) |
| Vertex AI (OpenAI-compat) | `aiplatform.googleapis.com/.../openapi` | gcloud OAuth + GCP project | Billed | Better, but needs gcloud + billing |
| **Cloud Code Assist** | `cloudcode-pa.googleapis.com` | gemini-cli OAuth | **Free tier** | Full support including thoughtSignature replay |

**This guide wires hermes to Cloud Code Assist** through a new `gemini-oauth` provider that mirrors the existing `qwen-oauth` pattern.

---

## What you get

When you finish this guide:

- `hermes` runs Gemini 3 (`gemini-3-flash-preview` by default) end-to-end with full tool calling
- A new `gemini-oauth` provider that reads OAuth tokens from `~/.gemini/oauth_creds.json` (managed by Google's `gemini` CLI — hermes refreshes them in place)
- A custom `GoogleCodeAssistClient` that handles `thoughtSignature` replay, project discovery, and retry-on-429
- Aliases: `google-oauth`, `gemini-cli`, `google-gemini-cli` all resolve to the same provider
- 36+ unit tests mirroring the Qwen provider tests, covering token reads/writes, refresh round-trips, alias resolution, and registry consistency
- **1 million token context** (1,048,576 tokens) properly configured — hermes knows the full Gemini context window for compression timing and request validation
- Existing hermes providers (Anthropic, OpenAI, Qwen, Codex, etc.) keep working unchanged

---

## Prerequisites

1. **Python 3.11** — hermes uses 3.11 specifically; check with `python3.11 --version`
2. **Google's `gemini` CLI** — install from <https://github.com/google-gemini/gemini-cli> (`npm i -g @google/gemini-cli` or your distro's package). Used **once** to mint the OAuth tokens.
3. **A Google account** — any, including a personal Gmail. The free Code Assist tier auto-provisions on first call.
4. **`hermes-agent` checked out** — `git clone https://github.com/nousresearch/hermes-agent.git ~/.hermes/hermes-agent`

> Hermes' runtime convention is two layers:
> - `~/.hermes/` holds runtime data (`auth.json`, `config.yaml`, `sessions/`, `logs/`, `sandboxes/`, etc.)
> - `~/.hermes/hermes-agent/` holds the source tree (the cloned git repo)
>
> The `hermes` CLI binary lives at `~/.local/bin/hermes` with a shebang pointing at `~/.hermes/hermes-agent/venv/bin/python3`.

---

## TL;DR (for the impatient)

```bash
# 1. Mint Google OAuth tokens via the official CLI (one-time browser flow)
gemini      # follow the device-code prompt → writes ~/.gemini/oauth_creds.json

# 2. Clone hermes (if you haven't already)
git clone https://github.com/nousresearch/hermes-agent.git ~/.hermes/hermes-agent
cd ~/.hermes/hermes-agent
python3.11 -m venv venv
source venv/bin/activate
pip install -e .

# 3. Apply the gemini-oauth provider patches described in this guide
#    (see "How the integration works" below)

# 4. Adopt the gemini credentials into hermes's pool
hermes auth add gemini-oauth

# 5. Set gemini-oauth as the default in ~/.hermes/config.yaml
#    model.default: gemini-3-flash-preview
#    model.provider: gemini-oauth

# 6. Test it
hermes chat -q "use the memory tool to remember 'I like synthwave'"
```

That's the happy path. The rest of this README is the **how it works** + **how to build it from scratch**.

---

## Architecture overview

Hermes' provider system is split across several files. Adding a new OAuth provider means touching all of them in a coordinated way:

```
hermes_cli/
├── auth.py                 ← Provider registry, helpers, alias map
├── auth_commands.py        ← `hermes auth add` CLI dispatch
├── runtime_provider.py     ← Runtime credential resolution per provider
└── main.py                 ← Interactive setup wizard, model menus

agent/
├── model_metadata.py       ← Model name → provider prefix mapping
├── models_dev.py           ← Provider → models.dev id mapping
├── google_codeassist_client.py     ← OpenAI-shaped wrapper
├── google_codeassist_protocol.py   ← Translation + signature cache
└── google_codeassist_project.py    ← Project discovery + persistence

run_agent.py                ← Branches on provider name to construct the right client
~/.hermes/config.yaml       ← Default model + provider selection
tests/hermes_cli/test_auth_gemini_provider.py    ← New test file
```

Each of these gets a small surgical change. The full file list is **9 files touched**, ~500 lines of new code, and ~36 new tests.

---

## Step-by-step setup

### Step 1 — Mint Google OAuth tokens via the `gemini` CLI

```bash
# Install Google's official gemini CLI
npm install -g @google/gemini-cli   # or your distro's package

# Run it once to trigger the OAuth flow
gemini
```

Sign in via the browser. Confirm the file landed:

```bash
ls -la ~/.gemini/oauth_creds.json
# -rw------- ... oauth_creds.json
```

If perms aren't `0600`, fix it: `chmod 600 ~/.gemini/oauth_creds.json`.

### Step 2 — Install hermes from source

```bash
git clone https://github.com/nousresearch/hermes-agent.git ~/.hermes/hermes-agent
cd ~/.hermes/hermes-agent
python3.11 -m venv venv
source venv/bin/activate
pip install -e .
```

Smoke-test:

```bash
hermes --version
hermes auth list
```

### Step 3 — Apply the `gemini-oauth` provider patches

The full set of edits is documented in **How the integration works** below. Apply them to:

- `hermes_cli/auth.py` — constants, ProviderConfig entry, 7 helper functions, aliases
- `hermes_cli/auth_commands.py` — provider id added to `_OAUTH_CAPABLE_PROVIDERS`, branch in `auth_add_command`, alias normalization
- `hermes_cli/runtime_provider.py` — branch in `resolve_runtime_credentials_for_model`
- `hermes_cli/main.py` — `provider_labels`, `top_providers`, dispatch, `_model_flow_gemini_oauth`
- `agent/model_metadata.py` — provider id added to `_PROVIDER_PREFIXES`
- `agent/models_dev.py` — `gemini-oauth → google` mapping
- `agent/google_codeassist_client.py` — new file (OpenAI-shaped wrapper)
- `agent/google_codeassist_protocol.py` — new file (translation + signature cache)
- `agent/google_codeassist_project.py` — new file (project discovery)
- `run_agent.py` — branch on `self.provider == "gemini-oauth"` in `_create_openai_client`
- `tests/hermes_cli/test_auth_gemini_provider.py` — new test file (mirror `test_auth_qwen_provider.py`)

### Step 4 — Run the test suite

After applying the patches, run the auth tests:

```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate
python -m pytest tests/hermes_cli/test_auth_gemini_provider.py -v
python -m pytest tests/hermes_cli/ -v   # full auth suite — should be 100% green
```

The reference numbers from when this was first landed:
- 36/36 new gemini provider tests pass
- 101/101 full auth test suite passes (zero regressions on Qwen, Codex, etc.)
- 248/248 cross-cutting sweep across every touched test file

### Step 5 — Adopt the credentials into hermes's pool

```bash
hermes auth add gemini-oauth
```

This calls `_read_gemini_cli_tokens()` to validate `~/.gemini/oauth_creds.json` and registers the credential in hermes's auth pool. Verify:

```bash
hermes auth list gemini-oauth
# should show the credential as active
```

Aliases all resolve to the same provider:

```bash
hermes auth add google-oauth         # ✓
hermes auth add gemini-cli           # ✓
hermes auth add google-gemini-cli    # ✓
hermes auth add google-cli           # ✓
hermes auth add gemini-oauth         # ✓ canonical
```

### Step 6 — Set as default

Edit `~/.hermes/config.yaml`:

```yaml
model:
  default: gemini-3-flash-preview
  provider: gemini-oauth
providers:
  gemini-oauth:
    base_url: https://cloudcode-pa.googleapis.com
```

### Step 7 — Verify end-to-end

```bash
# Simple query
hermes chat -q "say hi in one sentence"

# Tool calling (the real test — exercises thoughtSignature replay)
hermes chat -q "use the memory tool to remember 'I like synthwave music' then briefly tell me what you remembered"

# Live refresh round-trip
python -c "
from hermes_cli.auth import resolve_gemini_runtime_credentials
creds = resolve_gemini_runtime_credentials(force_refresh=True)
print('Got fresh ya29 token:', creds['api_key'][:24] + '...')
"
```

If memory-tool calling works without a 400 error, you're done.

---

## How the integration works

This section describes each file change in detail. The Gemini implementation is a **near-line-for-line mirror of the Qwen provider**, so future devs can compare them side-by-side.

### `hermes_cli/auth.py` — constants + provider entry + 7 helper functions

**Constants block** (near the top of the file, alongside `QWEN_OAUTH_*`):

```python
# Embedded gemini-cli OAuth credentials (NOT secret — every gemini-cli install
# ships these. Scoped to the Code Assist API surface only.)
GEMINI_OAUTH_CLIENT_ID = "<GOOGLE_CLIENT_ID>"
GEMINI_OAUTH_CLIENT_SECRET = "<GOOGLE_CLIENT_SECRET>"
GEMINI_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GEMINI_OAUTH_REFRESH_SKEW_SECONDS = 120
```

**`PROVIDER_REGISTRY` entry**:

```python
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
```

**`_PROVIDER_ALIASES` entries**:

```python
"gemini-oauth": "gemini-oauth",
"gemini-cli": "gemini-oauth",
"google-oauth": "gemini-oauth",
"google-gemini-cli": "gemini-oauth",
"google-cli": "gemini-oauth",
```

**The 7 helper functions** (mirror Qwen's exactly, just rename `qwen` → `gemini`):

```python
def _gemini_cli_auth_path() -> Path:
    return Path.home() / ".gemini" / "oauth_creds.json"

def _read_gemini_cli_tokens() -> Dict[str, Any]:
    """Read tokens from ~/.gemini/oauth_creds.json — returns dict or raises AuthError."""
    path = _gemini_cli_auth_path()
    if not path.exists():
        raise AuthError(provider="gemini-oauth", message=f"missing {path}")
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise AuthError(provider="gemini-oauth", message=f"invalid JSON in {path}: {e}")

def _save_gemini_cli_tokens(tokens: Dict[str, Any]) -> None:
    """Write tokens back to ~/.gemini/oauth_creds.json with chmod 600."""
    path = _gemini_cli_auth_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(tokens, f, indent=2)
    os.chmod(path, 0o600)

def _gemini_access_token_is_expiring(tokens: Dict[str, Any]) -> bool:
    """True if the access_token is within REFRESH_SKEW_SECONDS of expiry."""
    expiry_ms = tokens.get("expiry_date", 0)
    now_ms = int(time.time() * 1000)
    return (expiry_ms - now_ms) < (GEMINI_OAUTH_REFRESH_SKEW_SECONDS * 1000)

def _refresh_gemini_cli_tokens(tokens: Dict[str, Any]) -> Dict[str, Any]:
    """POST to oauth2.googleapis.com/token to mint a fresh access_token."""
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise AuthError(provider="gemini-oauth", message="no refresh_token in oauth_creds.json")

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
        raise AuthError(
            provider="gemini-oauth",
            message=f"refresh failed: {response.status_code} {response.text}",
        )
    body = response.json()
    new_tokens = dict(tokens)
    new_tokens["access_token"] = body["access_token"]
    new_tokens["expiry_date"] = int(time.time() * 1000) + body.get("expires_in", 3599) * 1000
    return new_tokens

def resolve_gemini_runtime_credentials(force_refresh: bool = False) -> Dict[str, Any]:
    """Top-level entry point used by runtime_provider.py."""
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
    except AuthError as e:
        return {"active": False, "error": str(e)}
```

### `hermes_cli/auth_commands.py` — CLI dispatch

Add `gemini-oauth` to the OAuth-capable set:

```python
_OAUTH_CAPABLE_PROVIDERS = {
    "qwen-oauth",
    "nous-portal",
    "openai-codex",
    "gemini-oauth",   # <-- NEW
}
```

Add a branch in `auth_add_command` (copy the Qwen branch verbatim, replace `qwen` → `gemini`):

```python
elif normalized_provider == "gemini-oauth":
    try:
        creds = _read_gemini_cli_tokens()
    except AuthError as e:
        click.echo(f"Cannot adopt gemini credentials: {e}", err=True)
        return
    # Register in hermes's auth pool (same shape as qwen)
    auth_pool.add_credential(
        provider="gemini-oauth",
        credential=creds,
        source=str(_gemini_cli_auth_path()),
    )
    click.echo("✓ adopted gemini OAuth credentials from ~/.gemini/oauth_creds.json")
```

Add aliases to `_normalize_provider`:

```python
ALIASES = {
    # ... existing ...
    "gemini-cli": "gemini-oauth",
    "google-oauth": "gemini-oauth",
    "google-gemini-cli": "gemini-oauth",
    "google-cli": "gemini-oauth",
}
```

> The alias map exists in TWO places — `auth.py` and `auth_commands.py`. Update **both** when adding new ones, or alias resolution will silently break in one of the dispatch paths.

### `hermes_cli/runtime_provider.py` — runtime dispatch

Add a branch alongside Qwen in `resolve_runtime_credentials_for_model`:

```python
from hermes_cli.auth import resolve_gemini_runtime_credentials

def resolve_runtime_credentials_for_model(model_name: str, provider: str) -> Dict[str, Any]:
    # ... existing branches ...
    elif provider == "gemini-oauth":
        return resolve_gemini_runtime_credentials()
```

### `hermes_cli/main.py` — interactive setup wizard

Add to provider labels and menus:

```python
provider_labels = {
    # ... existing ...
    "gemini-oauth": "Google Gemini OAuth (Code Assist, free tier)",
}

top_providers = [
    "anthropic",
    "openai",
    "gemini-oauth",  # <-- NEW (high billing — top of menu)
    "qwen-oauth",
    # ...
]
```

Add the dispatch case:

```python
elif selected_provider == "gemini-oauth":
    return _model_flow_gemini_oauth()
```

Add the model flow function (curated default list since Google's OpenAI-compat endpoint doesn't expose `/models`):

```python
def _model_flow_gemini_oauth() -> str:
    """Pick a Gemini model. Curated list because Code Assist has no /models endpoint."""
    options = [
        ("gemini-3-flash-preview", "Gemini 3 Flash Preview (default — fast, thinking-mode capable)"),
        ("gemini-3-pro", "Gemini 3 Pro (slow, lower quota)"),
        ("gemini-2.5-flash", "Gemini 2.5 Flash (proven)"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro"),
    ]
    return _prompt_choice("Pick a Gemini model:", options)
```

### `agent/model_metadata.py` — model name parsing

Add to the provider prefix set:

```python
_PROVIDER_PREFIXES = frozenset({
    # ... existing ...
    "gemini-oauth",
    "google-gemini-cli",
})
```

### `agent/models_dev.py` — context length resolution

Add the provider mapping:

```python
PROVIDER_TO_MODELS_DEV = {
    # ... existing ...
    "gemini-oauth": "google",
}
```

### `agent/google_codeassist_*.py` — the actual client

Three new files. Their job:

- **`google_codeassist_project.py`** (~370 lines) — project discovery via `:loadCodeAssist` and `:onboardUser` LRO polling, persists `project_id` back into `~/.gemini/oauth_creds.json`
- **`google_codeassist_protocol.py`** (~800 lines) — OpenAI ↔ Code Assist envelope translation, `StreamState` for SSE parsing, `_SIGNATURE_CACHE` (module-level dict, see below)
- **`google_codeassist_client.py`** (~630 lines) — drop-in `openai.OpenAI`-shaped client with `client.chat.completions.create(...)` API, owns retry-on-429 with quota-hint parsing

**Critical pattern in `google_codeassist_protocol.py`:**

```python
import threading

# Module-level signature cache (NOT per-instance — see Step 4 of the
# thought-signature gotcha section below for why)
_SIGNATURE_CACHE: dict[str, str] = {}
_SIGNATURE_CACHE_LOCK = threading.Lock()

def capture_signature(tool_call_id: str, signature: str) -> None:
    with _SIGNATURE_CACHE_LOCK:
        _SIGNATURE_CACHE[tool_call_id] = signature

def lookup_signature(tool_call_id: str) -> str | None:
    with _SIGNATURE_CACHE_LOCK:
        return _SIGNATURE_CACHE.get(tool_call_id)

def lookup_signature_by_payload(name: str, canonical_args: str) -> str | None:
    """Fallback when tool_call_id was rewritten between turns."""
    key = f"{name}::{canonical_args}"
    with _SIGNATURE_CACHE_LOCK:
        return _SIGNATURE_CACHE.get(key)
```

### `run_agent.py` — branch on provider name

In `_create_openai_client` (or `_create_request_openai_client`), add a branch:

```python
def _create_openai_client(self):
    if self.provider == "gemini-oauth":
        from agent.google_codeassist_client import GoogleCodeAssistClient
        return GoogleCodeAssistClient(api_key=self.creds["api_key"])
    # ... existing branches for openai, anthropic, qwen-oauth, etc. ...
    return openai.OpenAI(base_url=self.base_url, api_key=self.api_key)
```

Important: hermes calls `_create_request_openai_client(shared=False)` for **every** API request, which is why the signature cache must be **module-level**, not per-instance.

### `tests/hermes_cli/test_auth_gemini_provider.py` — new test file

Copy `test_auth_qwen_provider.py` verbatim, then:

1. Replace `qwen` → `gemini` everywhere
2. Replace `~/.qwen/oauth_creds.json` → `~/.gemini/oauth_creds.json`
3. Replace the Qwen token URL → `https://oauth2.googleapis.com/token`
4. Replace the embedded client_id/secret with the gemini ones
5. Update the expected `base_url` → `https://cloudcode-pa.googleapis.com`

You'll end up with ~36 tests covering: file read/write, missing file errors, invalid JSON errors, expiry detection, refresh round-trip, alias resolution, registry consistency.

### `~/.hermes/config.yaml` — set as default

```yaml
model:
  default: gemini-3-flash-preview
  provider: gemini-oauth
providers:
  gemini:
    base_url: https://generativelanguage.googleapis.com/v1beta/openai
    api_key_env: GOOGLE_API_KEY
  gemini-oauth:
    base_url: https://cloudcode-pa.googleapis.com
```

Note: keep the `gemini` (api_key) provider entry separate from `gemini-oauth` so users who want the API-key path can still use it.

---

## The `thoughtSignature` gotcha (THE most important non-obvious thing)

Skip this and your second tool call will return a 400. Got bitten by it twice — once in claw-code, once in hermes during the original landing.

**The problem:** Gemini 3 (and `gemini-2.5-pro` w/ thinking enabled) emits a `thoughtSignature` field whenever it produces a `functionCall` part. On the **next turn**, when you replay that assistant message as conversation history, you **MUST** include the original `thoughtSignature` on the same `functionCall` part. Google verifies it server-side. Miss it → 400.

```
400 Bad Request: Unable to submit request because function call `X` in the N. content block
is missing a `thought_signature`. Learn more: https://...
```

**Five subtleties that bit us:**

1. **The signature can appear on ANY part type**, not just `functionCall`. Per Google's docs: "For non-functionCall responses, the signature appears on the last part for context replay." Track the latest non-empty signature seen across **all** parts in a response and let any sibling functionCall claim it.

2. **The signature lives on the part itself**, not inside `functionCall`. So `part.thoughtSignature`, not `part.functionCall.thoughtSignature`.

3. **Per-instance state is wrong for hermes.** Hermes' `run_agent` calls `_create_openai_client` with `shared=False` for every API request. A per-instance signature cache will be empty on every replay. **Use module-level state** (the `_SIGNATURE_CACHE` dict shown above).

4. **OpenAI's tool_call shape has no field for `thoughtSignature`.** The cache must be a side channel — capture on the response side, look up by `tool_call_id` when building the next request.

5. **Cache by both id AND payload.** Some agent layers rewrite `tool_call_id`s between turns. Falling back to a `name::canonical-args` key catches that case.

**Debug recipe** (for future regressions):

1. Confirm the cache is **module-level**, not per-instance
2. Add `[CA-DEBUG]` prints at the cache write site (response handling) — does the signature actually get captured?
3. Add the same prints at the cache read site (request building) — does the lookup succeed?
4. Check `tool_call_id` matches between turns
5. Verify the stream parser tracks signatures across **all** part types, not just `functionCall` parts

---

## Verifying the install

```bash
# Health check
hermes auth list gemini-oauth

# Live refresh round-trip
python -c "
from hermes_cli.auth import resolve_gemini_runtime_credentials
creds = resolve_gemini_runtime_credentials(force_refresh=True)
print('access_token:', creds['api_key'][:24] + '...')
print('expires_at_ms:', creds['expires_at_ms'])
print('source:', creds['source'])
"

# Simple chat
hermes chat -q "say hi in one sentence"

# Tool calling (the real test — exercises thoughtSignature replay)
hermes chat -q "use the memory tool to remember 'I like synthwave music' then briefly tell me what you remembered"
```

If memory-tool calling works without a 400, you're done. The thinking-mode replay is the failure mode you're testing for.

---

## Enabling 1 million token context

Gemini 3 and 2.5 models support up to **1,048,576 tokens** (1M) of context through the Code Assist API. Hermes doesn't get this automatically — you need to tell it. Without explicit configuration, hermes may fall back to a lower detected limit (128K from endpoint probing, or whatever `models.dev` reports) depending on which step in the resolution chain fires first.

### The quick fix

Add `context_length: 1048576` to the `model:` section of `~/.hermes/config.yaml`:

```yaml
model:
  default: gemini-3-flash-preview
  provider: gemini-oauth
  context_length: 1048576    # <-- THIS LINE
```

That's it. This is **step 0** in hermes' 10-step context length resolution chain — an explicit config override that takes priority over every other detection method.

### Why this matters

Without the explicit override, hermes resolves context length through a cascade:

1. **Config override** (`model.context_length`) — **wins immediately if set**
2. Persistent cache (previously discovered via probing)
3. Active endpoint metadata (`/models` query)
4. Local server query
5. Anthropic `/v1/models` API
6. OpenRouter live API metadata
7. Nous suffix-match via OpenRouter cache
8. `models.dev` registry lookup (provider-aware)
9. Hardcoded defaults (e.g. `"gemini": 1048576`)
10. Fallback — 128K

The Code Assist endpoint (`cloudcode-pa.googleapis.com`) **does not expose a `/models` endpoint**, so steps 2-3 return nothing. Steps 5-7 don't apply to Gemini OAuth. Step 8 (`models.dev`) *usually* returns the right value — but it depends on `models.dev` being reachable, the model being listed, and the provider mapping being correct. Step 9 has the right hardcoded default (`"gemini": 1048576`), but that's far down the chain and subject to fuzzy matching.

Setting `context_length` in config.yaml **short-circuits all of that** and guarantees hermes knows the full window.

### What 1M context actually enables

With the full 1M window, hermes can:

- **Hold longer conversations** without triggering compression — at the default compression threshold of 50%, hermes won't compress until ~500K tokens of prompt
- **Ingest entire codebases** in a single session — a large repo's worth of file reads fits comfortably
- **Preserve more context post-compression** — hermes' compression keeps a "recent tail" of `target_ratio * threshold * context_length`. At 1M, that's `0.20 * 0.50 * 1048576 = ~100K tokens` of recent conversation preserved after compression, vs ~12.8K at 128K context
- **Multi-turn tool calling** with large tool results — tool outputs from file reads, web fetches, and code execution don't crowd out conversation history as quickly

### Compression tuning for 1M context

Hermes' automatic compression is context-length-aware. With 1M context, the defaults work well:

```yaml
compression:
  enabled: true
  threshold: 0.5       # compress when prompt hits 50% of context (500K tokens)
  target_ratio: 0.2    # keep 20% of the threshold window as recent tail (~100K)
  protect_last_n: 20   # always keep the last 20 messages intact
  summary_model: google/gemini-3-flash-preview   # use the same cheap model to summarize
```

If you find compression triggering too often (or not often enough), tune `threshold`. A value of `0.7` would let conversations grow to ~700K tokens before compressing.

### The hardcoded fallback

Even without the config override, hermes has a hardcoded default in `agent/model_metadata.py`:

```python
DEFAULT_CONTEXT_LENGTHS = {
    # ...
    "gemini": 1048576,
    # ...
}
```

This catches any model whose name contains `"gemini"` — so `gemini-3-flash-preview`, `gemini-2.5-pro`, etc. all match. But this is step 9 out of 10 in the resolution chain, and fuzzy matching can have edge cases. The explicit config is more reliable and self-documenting.

### Verify it's working

Check what context length hermes is actually using:

```bash
# In a hermes session, the startup log shows the resolved context length.
# Look for a line like:
#   Context length: 1,048,576 tokens

# Or verify programmatically:
python -c "
from agent.model_metadata import get_model_context_length
ctx = get_model_context_length(
    'gemini-3-flash-preview',
    config_context_length=1048576,
    provider='gemini-oauth',
)
print(f'Context length: {ctx:,} tokens')
"
# Expected: Context length: 1,048,576 tokens
```

If you see 128,000 instead of 1,048,576, the config override isn't being picked up — double-check that `context_length` is nested under `model:` (not at the top level).

### For claw-code (Rust)

In `claw-code`, Gemini models are **not listed** in the explicit `model_token_limit()` registry, which means they skip context-window validation entirely and let the API handle it natively. No configuration needed — 1M context works out of the box. The difference is architectural: claw-code trusts the API, hermes validates locally.

> For a deeper dive on how context resolution works and edge cases to watch for, see [`docs/context-1m.md`](docs/context-1m.md).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `400: missing thought_signature` | The cache isn't being populated or replayed | Verify `_SIGNATURE_CACHE` is module-level (in `google_codeassist_protocol.py`), not per-instance. Add `[CA-DEBUG]` prints at cache write/read. |
| `401 Unauthorized` | Access token expired and refresh failed | Run `gemini` once to re-mint. Check `~/.gemini/oauth_creds.json` has a `refresh_token`. |
| `403 Permission Denied` on project discovery | Account hasn't been onboarded to Code Assist free tier | Run `gemini` once — Google's CLI also triggers onboarding. Or check `agent/google_codeassist_project.py::onboard_user` is being called. |
| `429 Quota Exceeded` | Free tier rate limit (~5 RPM for Flash) | The client parses `retryDelay` from the response and sleeps. Check the inner retry actually waits. |
| `hermes auth add gemini-oauth` fails with `missing ~/.gemini/oauth_creds.json` | Google's `gemini` CLI hasn't been run yet | Run `gemini` once first. |
| `hermes` keeps using `openai` instead of `gemini-oauth` | `~/.hermes/config.yaml` still points elsewhere | Set `model.provider: gemini-oauth` in config.yaml. |
| Test `test_auth_gemini_provider.py` fails | Token URL or client_id mismatch | Compare line-by-line against `test_auth_qwen_provider.py` — same shape, just gemini constants. |
| Context shows 128K instead of 1M | Config override missing or misplaced | Add `context_length: 1048576` under `model:` in `~/.hermes/config.yaml` (not at root level). See [Enabling 1M context](#enabling-1-million-token-context). |

---

## Repository contents

```
.
├── README.md                          ← you are here
├── LICENSE
├── docs/
│   ├── wire-protocol.md               ← Code Assist API request/response shape
│   ├── thought-signature.md           ← Deep dive on the thinking-mode replay fix
│   ├── pattern-mirror-qwen.md         ← Side-by-side: gemini provider vs qwen provider
│   └── context-1m.md                  ← Enabling and verifying 1M token context
├── examples/
│   ├── auth_py_diff.py                ← Annotated diff for hermes_cli/auth.py
│   ├── auth_commands_diff.py          ← Annotated diff for hermes_cli/auth_commands.py
│   ├── runtime_provider_diff.py       ← Annotated diff for runtime_provider.py
│   ├── google_codeassist_client.py    ← Sketch of the client wrapper
│   └── google_codeassist_protocol.py  ← Sketch of the protocol translator + cache
└── scripts/
    ├── verify-install.sh              ← Smoke-test the integration
    └── refresh-token.sh               ← Standalone refresh-token helper for debugging
```

---

## Credits + references

- [`nousresearch/hermes-agent`](https://github.com/nousresearch/hermes-agent) — the upstream this guide targets
- Google's official [`gemini` CLI](https://github.com/google-gemini/gemini-cli) — the source of the OAuth tokens and the wire protocol reference
- [Nous Research's pi-ai library](https://github.com/NousResearch/pi-ai) — original reference implementation for the embedded OAuth credentials
- The `qwen-oauth` provider in hermes — the canonical pattern this implementation mirrors

## License

MIT — see [LICENSE](LICENSE).
