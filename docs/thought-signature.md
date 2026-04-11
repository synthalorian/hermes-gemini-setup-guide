# The `thoughtSignature` Replay Fix (Hermes/Python)

This is the **single most important non-obvious thing** about the Code Assist API. Skip the fix and your second tool call will return:

```
400 Bad Request: Unable to submit request because function call `X` in the N. content block
is missing a `thought_signature`. Learn more: https://...
```

The error message says "missing token" — it's actually about thinking-mode replay verification.

## The bug

Gemini 3 (and `gemini-2.5-pro` w/ thinking enabled) emits a `thoughtSignature` field whenever it produces a `functionCall` part. On the **next turn**, when you replay that assistant message as conversation history, you **MUST** include the original `thoughtSignature` on the same `functionCall` part.

If you're translating between OpenAI's tool-call shape (which has no signature field) and Code Assist's content-part shape, the signature gets dropped on the floor unless you cache it as a side channel.

## Five subtleties that bit us

### 1. The signature can appear on ANY part type

Per Google's documentation: *"For non-functionCall responses, the signature appears on the last part for context replay."*

A response might look like:

```python
{
  "parts": [
    {"text": "Let me check that for you."},
    {"thought": True, "text": "I should call the bash tool..."},
    {"functionCall": {"name": "bash", "args": {"cmd": "ls"}}},
    {"thoughtSignature": "abc123..."}
  ]
}
```

The signature is on the **fourth** part, but it belongs to the **third** part's functionCall. **Track the latest non-empty signature seen across ALL parts in a response**, and let any sibling functionCall claim it.

```python
def parse_response_parts(parts: list[dict]) -> None:
    latest_signature: str | None = None
    for part in parts:
        sig = part.get("thoughtSignature")
        if sig:
            latest_signature = sig
        if "functionCall" in part:
            tool_call_id = derive_tool_call_id(part)
            signature = part.get("thoughtSignature") or latest_signature
            if signature:
                capture_signature(tool_call_id, signature)
```

### 2. The signature lives on the part itself

Not inside `functionCall`:

```python
✓ part["thoughtSignature"]
✗ part["functionCall"]["thoughtSignature"]
```

### 3. Per-instance state breaks the cache

Hermes' `run_agent` calls `_create_request_openai_client(shared=False)` for **every** API request. A per-instance cache will be **empty on every replay**.

**Use module-level state:**

```python
# In agent/google_codeassist_protocol.py
import threading

_SIGNATURE_CACHE: dict[str, str] = {}
_SIGNATURE_CACHE_LOCK = threading.Lock()

def capture_signature(tool_call_id: str, signature: str) -> None:
    with _SIGNATURE_CACHE_LOCK:
        _SIGNATURE_CACHE[tool_call_id] = signature

def lookup_signature(tool_call_id: str) -> str | None:
    with _SIGNATURE_CACHE_LOCK:
        return _SIGNATURE_CACHE.get(tool_call_id)
```

The lock matters because hermes runs gateway connectors (telegram, discord, etc.) on multiple threads.

### 4. OpenAI's tool_call shape has no field for signatures

The cache must be a side channel:

- **Capture** on the response side, keyed by `tool_call_id`
- **Look up** by `tool_call_id` when building the next request body

The OpenAI `ChatCompletionMessageToolCall` type doesn't have a place to put a `thoughtSignature`, so we keep it in the cache and re-attach when translating back to Code Assist's wire format.

### 5. Cache by both id AND payload

Some agent layers (LangChain, custom runtimes) rewrite `tool_call_id`s between turns. To survive that, cache under **both** the id AND a canonical `(name, args)` payload key:

```python
def capture_signature(tool_call_id: str, name: str, args: dict, signature: str) -> None:
    with _SIGNATURE_CACHE_LOCK:
        _SIGNATURE_CACHE[tool_call_id] = signature
        canonical_args = json.dumps(args, sort_keys=True)
        _SIGNATURE_CACHE[f"{name}::{canonical_args}"] = signature

def lookup_signature(tool_call_id: str, name: str, args: dict) -> str | None:
    with _SIGNATURE_CACHE_LOCK:
        sig = _SIGNATURE_CACHE.get(tool_call_id)
        if sig:
            return sig
        canonical_args = json.dumps(args, sort_keys=True)
        return _SIGNATURE_CACHE.get(f"{name}::{canonical_args}")
```

When replaying, look up id first, then fall back to the payload key.

## How hermes implements it

The full implementation lives in `agent/google_codeassist_protocol.py`. The shape is:

```python
# Module-level cache + lock (NOT per-instance)
_SIGNATURE_CACHE: dict[str, str] = {}
_SIGNATURE_CACHE_LOCK = threading.Lock()


class StreamState:
    """Per-stream parser state — tracks the latest signature across all parts."""

    def __init__(self) -> None:
        self.latest_signature: str | None = None

    def observe_part(self, part: dict) -> None:
        sig = part.get("thoughtSignature")
        if sig:
            self.latest_signature = sig

    def claim_signature_for_function_call(
        self, part: dict, tool_call_id: str, name: str, args: dict
    ) -> None:
        signature = part.get("thoughtSignature") or self.latest_signature
        if signature:
            capture_signature(tool_call_id, name, args, signature)


def translate_assistant_message_to_codeassist(message: dict) -> dict:
    """When sending conversation history back to Code Assist, look up cached signatures."""
    parts = []
    for tool_call in message.get("tool_calls", []):
        sig = lookup_signature(
            tool_call["id"],
            tool_call["function"]["name"],
            json.loads(tool_call["function"]["arguments"]),
        )
        part = {
            "functionCall": {
                "name": tool_call["function"]["name"],
                "args": json.loads(tool_call["function"]["arguments"]),
            }
        }
        if sig:
            part["thoughtSignature"] = sig
        parts.append(part)
    return {"role": "model", "parts": parts}
```

## Debugging checklist

When you get a `400: missing thought_signature`:

1. **Confirm the cache is module-level**, not per-instance. Verify `_SIGNATURE_CACHE` is a top-level module variable in `google_codeassist_protocol.py`.
2. Add print statements at the cache **write** site (response handling) — does the signature actually get captured?
   ```python
   print(f"[CA-DEBUG] capture {tool_call_id[:16]}... → sig={signature[:24]}...")
   ```
3. Add print statements at the cache **read** site (request building) — does the lookup succeed?
   ```python
   print(f"[CA-DEBUG] lookup {tool_call_id[:16]}... → {'HIT' if sig else 'MISS'}")
   ```
4. Check the tool_call_id matches between turns — some agent layers mutate it
5. If id mismatch, verify the `(name, canonical_args)` fallback path works
6. Verify your stream parser tracks signatures across **all** part types, not just `functionCall` parts

## Reference: same fix in claw-code

The Rust implementation in `claw-code` uses `OnceLock<Mutex<HashMap<String, String>>>` for the same reason — process-wide state is required because the runtime constructs fresh clients per-request. Both implementations land at the same architectural answer.

If you're looking at one and want to compare, the Rust version is in `crates/api/src/providers/google_codeassist.rs` in the [`ultraworkers/claw-code`](https://github.com/ultraworkers/claw-code) tree (when the integration is present).
