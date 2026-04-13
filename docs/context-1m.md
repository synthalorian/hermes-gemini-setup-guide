# Enabling 1 Million Token Context for Gemini via Code Assist

Gemini 3 Flash, Gemini 3 Pro, and Gemini 2.5 Pro all support up to **1,048,576 tokens** (1M) of context through Google's Cloud Code Assist API. This document explains how hermes resolves context length, why Gemini OAuth needs an explicit override, and how to verify it's working correctly.

## The problem

Hermes needs to know the model's context window to:

1. **Decide when to compress conversation history** — compression triggers at a configurable percentage of the context length (default 50%)
2. **Calculate how much recent context to preserve** after compression — the "recent tail" budget is `target_ratio * threshold * context_length`
3. **Validate requests** — hermes can reject requests that would exceed the model's known limit before sending them to the API
4. **Reject models below the minimum** — hermes enforces a 64K minimum context length for tool-calling workflows

The Cloud Code Assist endpoint (`cloudcode-pa.googleapis.com`) **does not expose a `/v1/models` endpoint** or any metadata API that reports context length. This means hermes' auto-detection (which probes `/v1/models` on most providers) comes back empty for `gemini-oauth`. Without explicit configuration, hermes falls through a 10-step resolution chain and may land on a suboptimal value.

## The fix

Add one line to `~/.hermes/config.yaml`:

```yaml
model:
  default: gemini-3-flash-preview
  provider: gemini-oauth
  context_length: 1048576
```

`context_length` is the **total** context window — input + output tokens combined. It is **not** the output cap (that's `max_tokens`, a separate setting).

## How hermes resolves context length

The function `get_model_context_length()` in `agent/model_metadata.py` implements a 10-step resolution chain. Understanding the chain helps you debug context issues:

| Step | Source | Fires for gemini-oauth? | Notes |
|------|--------|------------------------|-------|
| 0 | `model.context_length` in config.yaml | **Yes — wins immediately** | This is the fix. Short-circuits everything below. |
| 1 | Persistent cache (from prior probing) | Only if previously cached | Cache is keyed by `(model, base_url)`. Empty on first run. |
| 2 | `/v1/models` endpoint metadata | **No** | Code Assist has no `/v1/models` endpoint. |
| 3 | Local server query | **No** | Only for `localhost`/`127.0.0.1` URLs. |
| 4 | Anthropic `/v1/models` API | **No** | Only for `provider == "anthropic"`. |
| 5 | OpenRouter live API | **No** | Only for OpenRouter-routed models. |
| 6 | Nous suffix-match | **No** | Only for `provider == "nous"`. |
| 7 | `models.dev` registry lookup | **Sometimes** | Requires `models.dev` to be reachable and the model to be listed. Provider mapping goes through `PROVIDER_TO_MODELS_DEV["gemini-oauth"] = "google"`. Works most of the time, but depends on network and registry freshness. |
| 8 | Hardcoded defaults | **Yes (fallback)** | `DEFAULT_CONTEXT_LENGTHS["gemini"] = 1048576`. Fuzzy match — any model name containing `"gemini"` hits this. |
| 9 | Default fallback | Last resort | Returns 128,000 (128K). |

**Without the config override**, the typical happy path for `gemini-oauth` is: steps 0-6 miss → step 7 (`models.dev`) returns the correct 1M → done. But if `models.dev` is unreachable (network issue, rate limit, DNS failure), it falls to step 8 (hardcoded `"gemini": 1048576`) which also returns 1M.

**With the config override**, step 0 fires immediately with 1,048,576 — no network calls, no fuzzy matching, no ambiguity.

## The hardcoded defaults

In `agent/model_metadata.py`:

```python
DEFAULT_CONTEXT_LENGTHS = {
    "claude-opus-4-6": 1000000,
    "claude-sonnet-4-6": 1000000,
    "claude": 200000,
    "gpt-4.1": 1047576,
    "gpt-5": 128000,
    "gpt-4": 128000,
    "gemini": 1048576,        # <-- catches all gemini-* models
    "gemma-4-31b": 256000,
    "gemma-3": 131072,
    "gemma": 8192,
    # ...
}
```

Matching is fuzzy: `default_model in model_lower`. So `"gemini"` matches `"gemini-3-flash-preview"`, `"gemini-2.5-pro"`, etc. Entries are sorted longest-key-first so `"gemma-4-31b"` matches before `"gemma"`.

This is a reliable fallback, but it's step 8 — seven other steps run first (and potentially return wrong values) before it's consulted. The config override at step 0 is more deterministic.

## How compression uses context length

Hermes' context compressor (`agent/context_compressor.py`) uses the resolved context length to decide when and how to compress:

```
compression_trigger = context_length * threshold
recent_tail_budget = context_length * threshold * target_ratio
```

With the default settings and 1M context:

| Setting | Value | Effect |
|---------|-------|--------|
| `threshold` | 0.5 | Compress when prompt tokens reach ~524K |
| `target_ratio` | 0.2 | Keep ~104K tokens of recent conversation after compression |
| `protect_last_n` | 20 | Always keep the last 20 messages intact regardless |

Compare with 128K context (the fallback if resolution fails):

| Setting | Value | Effect |
|---------|-------|--------|
| `threshold` | 0.5 | Compress at ~64K |
| `target_ratio` | 0.2 | Keep only ~12.8K tokens after compression |
| `protect_last_n` | 20 | Same |

The difference is dramatic. At 1M, you can have 500K tokens of conversation before compression triggers. At 128K, you hit the wall at 64K — which is a single large file read in many codebases.

### Tuning compression for large-context sessions

If you're doing deep codebase analysis and want to maximize context usage before compression:

```yaml
compression:
  enabled: true
  threshold: 0.7       # don't compress until 70% of 1M (~734K tokens)
  target_ratio: 0.25   # keep 25% of threshold as recent tail (~183K)
  protect_last_n: 30   # protect more recent messages
  summary_model: google/gemini-3-flash-preview
```

If you're doing many short tool-calling sessions and want aggressive compression to keep costs down:

```yaml
compression:
  enabled: true
  threshold: 0.3       # compress early at 30% (~314K tokens)
  target_ratio: 0.15   # smaller recent tail (~47K)
  protect_last_n: 15
  summary_model: google/gemini-3-flash-preview
```

## Verifying context length

### Check the resolved value programmatically

```bash
cd ~/.hermes/hermes-agent
source venv/bin/activate

python -c "
from agent.model_metadata import get_model_context_length

# With config override (what hermes actually uses)
ctx = get_model_context_length(
    'gemini-3-flash-preview',
    config_context_length=1048576,
    provider='gemini-oauth',
)
print(f'With config override:    {ctx:>12,} tokens')

# Without config override (resolution chain fallback)
ctx_auto = get_model_context_length(
    'gemini-3-flash-preview',
    provider='gemini-oauth',
)
print(f'Without config override: {ctx_auto:>12,} tokens')
"
```

Expected output:

```
With config override:    1,048,576 tokens
Without config override: 1,048,576 tokens
```

If the second line shows 128,000 instead of 1,048,576, the `models.dev` lookup and hardcoded default both missed — which means either the model name changed or the fuzzy match failed. The config override protects you from this.

### Check during a live session

Hermes logs the resolved context length at session startup. Look for:

```
Context length: 1,048,576 tokens
```

in the startup output or in `~/.hermes/logs/`.

### Check compression behavior

Start a hermes session and load a large file. Watch the token counter:

```bash
hermes chat -q "read the file ~/.hermes/hermes-agent/hermes_cli/auth.py and summarize it"
```

If context length is correctly set to 1M, this ~3100-line file should load without triggering compression. If it triggers compression on the first tool result, context length is probably resolving to 128K.

## claw-code comparison

In the Rust implementation (`claw-code`), Gemini models are handled differently:

- Gemini models are **not listed** in `model_token_limit()` at all
- This means `preflight_message_request()` skips context-window validation entirely
- The API itself handles context limits — if you exceed 1M tokens, Google returns an error
- No configuration needed

This is a simpler approach (trust the API) vs hermes' approach (validate locally). Both work. Hermes' approach gives better error messages and enables compression to trigger at the right time, but requires the explicit config line.

## Common mistakes

**1. Putting `context_length` at the wrong YAML nesting level**

Wrong:
```yaml
context_length: 1048576   # top-level — hermes won't see this
model:
  default: gemini-3-flash-preview
```

Right:
```yaml
model:
  default: gemini-3-flash-preview
  context_length: 1048576   # nested under model: — hermes reads this
```

**2. Confusing `context_length` with `max_tokens`**

- `context_length` = total window (input + output). Set to 1048576.
- `max_tokens` = output cap per response. The Code Assist client defaults to 16384. These are independent settings.

**3. Setting context length on the provider block instead of the model block**

Wrong:
```yaml
providers:
  gemini-oauth:
    base_url: https://cloudcode-pa.googleapis.com
    context_length: 1048576   # not read from here
```

Right:
```yaml
model:
  context_length: 1048576     # read from here
```

**4. Assuming `models.dev` always returns the right value**

The `models.dev` registry is a community-maintained resource. Model entries can be stale, missing, or report a different context limit than what the Code Assist API actually supports. The config override is authoritative.
