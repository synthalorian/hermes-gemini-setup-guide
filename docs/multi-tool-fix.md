# Multi-Tool-Call Response Grouping + Zero-Delay Retry Bug

Two bugs discovered during a live hermes session where the model called `read_file` and `find_files` in the same turn. The 429 retry mishandled a zero-second delay, and after retrying, the request hit a 400 because function responses weren't grouped correctly.

## Bug 1: Function response count mismatch (400)

### Error

```
400 Bad Request: Please ensure that the number of function response parts is equal
to the number of function call parts of the function call turn.
```

### Root cause

Google's Code Assist API requires that when the model emits N `functionCall` parts in a single `role: model` turn, the next `role: user` turn must contain exactly N `functionResponse` parts — all in one content block.

OpenAI's chat format represents tool results as separate messages:

```json
{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": "..."}
{"role": "tool", "tool_call_id": "call_2", "name": "find_files", "content": "..."}
```

The original translator in `google_codeassist_protocol.py` converted each `tool` message into its own content block:

```python
# BEFORE (broken for multi-tool turns)
part = {"functionResponse": {"name": fn_name, "response": response_data}}
contents.append({"role": "user", "parts": [part]})
```

This produced:

```json
[
  {"role": "model", "parts": [
    {"functionCall": {"name": "read_file", ...}},
    {"functionCall": {"name": "find_files", ...}}
  ]},
  {"role": "user", "parts": [{"functionResponse": {"name": "read_file", ...}}]},
  {"role": "user", "parts": [{"functionResponse": {"name": "find_files", ...}}]}
]
```

Two separate `role: user` blocks. Code Assist sees the first one, counts 1 response vs 2 calls, and rejects it.

### Fix

Merge consecutive `tool` messages into a single content block using an internal `_tool_responses` marker:

```python
# AFTER (groups multi-tool responses)
part = {"functionResponse": {"name": fn_name, "response": response_data}}
# Merge with previous content block if it's also a tool-response turn
if contents and contents[-1].get("role") == "user" and contents[-1].get("_tool_responses"):
    contents[-1]["parts"].append(part)
else:
    contents.append({"role": "user", "parts": [part], "_tool_responses": True})
```

The marker is stripped before the request is serialized:

```python
for block in contents:
    block.pop("_tool_responses", None)
```

This now produces:

```json
[
  {"role": "model", "parts": [
    {"functionCall": {"name": "read_file", ...}},
    {"functionCall": {"name": "find_files", ...}}
  ]},
  {"role": "user", "parts": [
    {"functionResponse": {"name": "read_file", ...}},
    {"functionResponse": {"name": "find_files", ...}}
  ]}
]
```

One `role: user` block with both responses. Count matches the call turn.

### Why the marker approach

The `_tool_responses` flag is needed because not every `role: user` block should absorb tool responses. Consider this sequence:

```
assistant: [tool_call A]
tool: [response A]
user: "okay now do B"
assistant: [tool_call B]
tool: [response B]
```

Without the flag, the translator might try to merge response B into the user message "okay now do B". The flag ensures only consecutive tool-response blocks get merged.

### Edge cases

- **Single tool call**: Works unchanged. One `functionCall`, one `functionResponse`, one block each.
- **Three or more tool calls**: All responses merge into one block. The loop appends to the same content block as long as the `_tool_responses` flag is present.
- **Interleaved user messages**: Break the chain correctly. A user message between tool results starts a new content block.
- **Empty tool results**: Still produce a `functionResponse` part with `{"result": ""}`. The count must match regardless of content.

## Bug 2: Zero-second retry delay (429 → random backoff)

### Error (observed behavior)

```
⚠️  API call failed (attempt 1/3): CodeAssistAPIError [HTTP 429]
   📝 Error: ... "Your quota will reset after 0s." ...
⏱️ Rate limit reached. Waiting 2.597858544360092s before retry (attempt 2/3)...
```

Google said "reset after 0s" (retry immediately) but hermes waited ~2.6 seconds from the outer retry layer's random backoff.

### Root cause

The delay parser in `_extract_retry_delay` correctly parsed `"after 0s"` and returned `0.0`. But the retry guard used Python truthiness:

```python
# BEFORE (broken)
if attempt < max_retries and retry_delay:  # 0.0 is falsy in Python!
```

`0.0` is falsy, so this evaluated to `False`. The internal retry was skipped entirely. The `CodeAssistAPIError(429)` propagated to the outer retry layer, which applied its own random exponential backoff (~2.6 seconds).

### Fix

Check for `None` instead of truthiness:

```python
# AFTER (fixed)
if attempt < max_retries and retry_delay is not None:  # 0.0 is a valid delay
```

Now `0.0` means "retry immediately" (correct), while `None` (unparseable response) would fall through to the error path (also correct, though in practice the parser always returns a float — it falls back to `12.0` when parsing fails).

### Why this matters

The `"reset after 0s"` response means Google's quota already reset by the time the error was returned. Waiting 2.6 seconds is harmless but unnecessary. More importantly, the wrong retry layer handled it — the outer layer doesn't know about Code Assist's quota semantics and might burn through its own retry budget on what should have been an instant recovery.

### The streaming path

The streaming retry loop in `_CodeAssistStreamIterator.__iter__()` doesn't have this bug — it checks `if attempt < max_retries:` without gating on the delay value. But it's worth auditing both paths whenever you touch retry logic.

## File changes summary

| File | Change |
|------|--------|
| `agent/google_codeassist_protocol.py` | Merge consecutive `tool` messages into single `role: user` content block; strip `_tool_responses` marker before serialization |
| `agent/google_codeassist_client.py` | Change `if retry_delay:` to `if retry_delay is not None:` in `_post_with_retry`; update `_extract_retry_delay` return type annotation to `Optional[float]` |

## How to reproduce

### Function response mismatch

Trigger any multi-tool turn. The easiest way:

```bash
hermes chat -q "read the file ~/.bashrc and also search for 'alias' in it"
```

If the model calls both `read_file` and `grep`/`find` in the same turn (likely), the second API request will hit the 400 if the fix isn't applied.

### Zero-delay retry

Harder to reproduce on demand — depends on hitting the quota at exactly the right moment. Run a burst of quick requests:

```bash
for i in {1..6}; do hermes chat -q "say '$i'" & done; wait
```

At ~5 RPM, requests 5-6 will likely get 429s. If Google returns `"reset after 0s"`, check the logs for the retry delay. With the fix, it should show `0.0s`. Without it, you'll see the outer layer's random backoff.
