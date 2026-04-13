# Handling Rate Limits on Google's Code Assist Free Tier

The Cloud Code Assist free tier enforces per-minute request quotas. When exceeded, the API returns HTTP 429 with hints about when your quota resets. This document covers how both hermes (Python) and claw-code (Rust) parse those hints and retry automatically, so the user's session pauses briefly instead of dying.

## Free tier quotas

Google doesn't publish exact numbers, but from observation:

| Model | Approximate RPM | Notes |
|-------|-----------------|-------|
| `gemini-3-flash-preview` | ~5 RPM | Higher than Pro, best for agent workflows |
| `gemini-3-pro` | ~2 RPM | Lower quota, slower responses |
| `gemini-2.5-flash` | ~5 RPM | Similar to 3 Flash |
| `gemini-2.5-pro` | ~2 RPM | Similar to 3 Pro |

These are rough estimates. The actual limits depend on your account, time of day, and whether Google has changed them since this was written. The important thing is that the retry logic adapts to whatever the server says, not to hardcoded assumptions.

## How Google signals the wait time

A 429 response from Code Assist carries the retry delay in two forms. Both can appear simultaneously; the structured form (`retryDelay`) is more reliable.

### Form 1: `retryDelay` in error details

```json
{
  "error": {
    "code": 429,
    "message": "Quota exceeded for quota metric 'generate_content_free_tier_input_token_count' ...",
    "status": "RESOURCE_EXHAUSTED",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "RATE_LIMIT_EXCEEDED",
        "domain": "googleapis.com"
      },
      {
        "retryDelay": "44s"
      }
    ]
  }
}
```

The `retryDelay` value is always a string ending in `"s"` (seconds). We've seen values ranging from `"8s"` to `"58s"`. Occasionally Google uses fractional seconds like `"44.123s"`.

### Form 2: Natural language in the error message

```json
{
  "error": {
    "message": "Your quota will reset after 38s."
  }
}
```

This appears in the `message` field and matches the pattern `after \d+s`. It's a fallback — sometimes present when `retryDelay` is not, sometimes both are present with slightly different values.

### Form 3: HTTP headers (rare from Code Assist)

Standard `Retry-After` and non-standard `X-RateLimit-Reset-After` headers. Code Assist doesn't consistently set these, but the implementations check them anyway for robustness.

## Hermes implementation (Python)

**File:** `agent/google_codeassist_client.py`

### Retry loop for non-streaming requests

`_post_with_retry()` wraps every non-streaming API call:

```python
def _post_with_retry(self, url, body, timeout, *, max_retries=3):
    client = self._get_http_client()
    for attempt in range(max_retries + 1):
        resp = client.post(url, headers=self._headers(), json=body, timeout=timeout)
        if resp.status_code == 429:
            retry_delay = self._extract_retry_delay(resp)
            if attempt < max_retries and retry_delay:
                logger.info(
                    "Code Assist 429, retrying in %.1fs (attempt %d/%d)",
                    retry_delay, attempt + 1, max_retries,
                )
                time.sleep(retry_delay)
                continue
        if resp.status_code >= 400:
            raise CodeAssistAPIError(
                f"Code Assist API error {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )
        return resp
    raise CodeAssistAPIError(
        "Code Assist API rate limited after max retries",
        status_code=429,
    )
```

### Retry loop for streaming requests

`_CodeAssistStreamIterator.__iter__()` has its own retry loop because `httpx`'s stream context manager must be re-entered on each attempt:

```python
def __iter__(self):
    http_client = self._client._get_http_client()
    state = StreamState(self._model)

    max_retries = 3
    for attempt in range(max_retries + 1):
        with http_client.stream("POST", self._url, ...) as resp:
            if resp.status_code == 429:
                resp.read()  # must consume body before parsing in stream mode
                retry_delay = self._client._extract_retry_delay(resp)
                if attempt < max_retries:
                    logger.info("Code Assist stream 429, retrying in %.0fs (%d/%d)",
                                retry_delay, attempt + 1, max_retries)
                    time.sleep(retry_delay)
                    continue
                raise CodeAssistAPIError(...)
            # ... parse SSE and yield chunks ...
            return  # success, don't retry
```

**Important:** `resp.read()` is called before parsing the error. In stream mode, the body isn't buffered automatically — without this call, `resp.json()` in `_extract_retry_delay` would fail.

### Delay parser

`_extract_retry_delay()` checks both forms:

```python
@staticmethod
def _extract_retry_delay(resp):
    try:
        data = resp.json()
        error = data.get("error", {})
        # Form 1: retryDelay in details
        for detail in error.get("details", []):
            delay_str = detail.get("retryDelay", "")
            if isinstance(delay_str, str) and delay_str.endswith("s"):
                return float(delay_str[:-1])
        # Form 2: "reset after Xs" in message
        import re
        msg = error.get("message", "")
        match = re.search(r"after\s+(\d+)s", msg)
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return 12.0  # default for ~5 RPM free tier
```

The 12-second default is a safe guess for ~5 RPM — one tick of the per-minute window. It's conservative enough to avoid hitting the limit again on the next attempt.

### Compatibility shim

The `_CodeAssistStreamIterator` exposes a `.response` attribute for compatibility with hermes' outer rate-limit header capture logic:

```python
class _CodeAssistStreamIterator:
    def __init__(self, ...):
        # Expose .response for compatibility with Hermes rate-limit header capture
        self.response = None
```

This is set to the actual `httpx.Response` once a successful stream is established. Without it, hermes' generic response-header inspection would crash on `NoneType`.

## claw-code implementation (Rust)

**File:** `crates/api/src/providers/google_codeassist.rs`

### Constants

```rust
const MAX_RETRIES: u32 = 3;
const BASE_BACKOFF_MS: u64 = 1500;      // exponential backoff for non-429 errors
const MAX_RETRY_WAIT_SECS: u64 = 90;    // cap on any single sleep
```

### Retry loop

`post_with_retry()` handles both streaming and non-streaming. It distinguishes between:

- **Network errors** (connection refused, timeout): exponential backoff (`1.5s → 3s → 6s`)
- **Server errors** (429, 500-504): server-guided wait if available, exponential backoff otherwise
- **Non-retryable errors** (400, 401, 403): fail immediately

```rust
async fn post_with_retry(&self, url: &str, headers: HeaderMap, body: &Value)
    -> Result<Response, ApiError>
{
    for attempt in 0..=MAX_RETRIES {
        let response = match self.http.post(url)...send().await {
            Ok(r) => r,
            Err(e) => {
                // Network error → exponential backoff
                let wait_ms = BASE_BACKOFF_MS * (1u64 << attempt);
                tokio::time::sleep(Duration::from_millis(wait_ms)).await;
                continue;
            }
        };

        if response.status().is_success() { return Ok(response); }

        if !is_retryable_status(status) || attempt >= MAX_RETRIES {
            return Err(ApiError::Api { ... });
        }

        // Server-guided wait (capped at 90s), or exponential backoff
        let server_wait = extract_retry_after_seconds(&headers, &body_text)
            .map(|s| s.min(MAX_RETRY_WAIT_SECS as f64));
        let wait_secs = server_wait.unwrap_or_else(|| {
            (BASE_BACKOFF_MS * (1u64 << attempt)) as f64 / 1000.0
        });
        tokio::time::sleep(Duration::from_millis((wait_secs * 1000.0) as u64)).await;
    }
}
```

### Retryable statuses

```rust
fn is_retryable_status(status: StatusCode) -> bool {
    matches!(status.as_u16(), 408 | 429 | 500 | 502 | 503 | 504)
}
```

This is broader than hermes' Python implementation (which only retries 429). The Rust side also handles server errors (500-504) and request timeouts (408), using exponential backoff when Google doesn't provide a `retryDelay`.

### Delay extractor

`extract_retry_after_seconds()` checks three sources in priority order:

1. **`Retry-After` header** — standard HTTP, parsed as float seconds
2. **`X-RateLimit-Reset-After` / `X-RateLimit-Reset` headers** — non-standard but common
3. **Response body patterns:**
   - `"retryDelay": "44.123s"` — parsed via ad-hoc string matching (avoids pulling in the `regex` crate)
   - `"reset after 44s"` — natural language pattern

The ad-hoc parser (`regex_capture`) avoids adding a `regex` dependency. It finds the `"retryDelay"` needle in the body text and parses the number + unit suffix forward from there:

```rust
fn regex_capture(pattern: &str, text: &str) -> Option<(String, Option<String>)> {
    // For the retryDelay pattern: find the needle, skip whitespace/colons/quotes,
    // accumulate digits, read unit suffix (s/ms)
    let needle = "\"retryDelay\"";
    let idx = text.find(needle)?;
    let after = &text[idx + needle.len()..];
    let after = after.trim_start_matches(|c: char| c == ':' || c.is_whitespace() || c == '"');
    // ... parse digits and unit ...
}
```

Both body patterns add a **+1 second buffer** to the parsed delay to avoid landing right on the quota edge:

```rust
if secs > 0.0 {
    return Some(secs + 1.0);  // buffer to avoid edge
}
```

### Max wait cap

The Rust implementation caps any single wait at 90 seconds:

```rust
let server_wait = extract_retry_after_seconds(&headers, &body_text)
    .map(|s| s.min(MAX_RETRY_WAIT_SECS as f64));
```

If Google says "wait 300s", something is seriously wrong. Better to fail fast and let the user know than to silently block for 5 minutes.

## Design decisions

### Why server-guided retry instead of fixed backoff?

Google tells you exactly when the quota resets. A fixed 30-second backoff either:
- **Wastes time** when the quota resets in 8 seconds
- **Retries too early** when the quota resets in 44 seconds, burning another attempt

Parsing the server's hint gives you the optimal wait time every time.

### Why 3 retries max?

Three retries is enough to survive a burst of quick requests hitting the quota simultaneously (e.g., tool calls that fire in rapid succession). It's not so many that a persistent quota issue keeps the user silently blocked for minutes.

With a 44-second quota wait, 3 retries means worst case ~2.5 minutes of total wait time. That's noticeable but acceptable for a free tier. If all 3 fail, the error surfaces to the user immediately.

### Why a 12-second default (Python)?

At ~5 RPM, the per-minute window has ~12-second intervals. If the response body can't be parsed, 12 seconds is the most likely wait for a single-request overshoot. It's conservative enough to avoid hitting the limit again, fast enough to not waste time.

### Why cap at 90 seconds (Rust)?

The longest quota wait we've observed is ~58 seconds. The 90-second cap gives headroom for unusual cases while preventing a pathological wait. If Google starts returning 300s delays, there's likely a deeper issue (account-level block, API change) that retry won't fix.

### Why retry 500-504 in Rust but not in Python?

Design difference, not a bug. The Python implementation is conservative — only retry the specific thing we know is transient (429). The Rust implementation is broader — server errors and timeouts from `cloudcode-pa.googleapis.com` are often transient (load balancer hiccups, regional failover). The exponential backoff (1.5s, 3s, 6s) for non-429 errors is short enough that the risk of wasted retries is minimal.

### Why separate retry loops for streaming and non-streaming (Python)?

`httpx`'s streaming API uses a context manager (`with client.stream(...) as resp:`). You can't re-enter the same context manager — you have to create a new one on each retry attempt. The non-streaming path just calls `client.post()` and gets the response back immediately. Different control flow, same retry logic.

## Avoiding retry storms

Hermes has its own outer retry logic in `run_agent.py`. If the outer retry fires on a 429 **before** the inner Code Assist retry finishes, you get doubled requests — which makes the rate limit situation worse.

**The rule:** The Code Assist client handles 429 retries internally (3 attempts with server-guided delays). Outer layers should treat a 429 from `GoogleCodeAssistClient` as **terminal** — it has already retried 3 times and the quota is still exceeded.

In practice, this means:
1. `GoogleCodeAssistClient._post_with_retry()` retries up to 3 times with parsed delays
2. If all 3 fail, it raises `CodeAssistAPIError(status_code=429)`
3. `run_agent.py` sees a 429 from the client and should **not** retry — the client already handled it
4. The error surfaces to the user or the agent's error handler

If you see doubled retry logs (one from the Code Assist client, one from the outer layer), check `run_agent.py`'s retry logic and ensure it doesn't retry 429s from `gemini-oauth`.

## Monitoring rate limit behavior

### Hermes logs

Look for these patterns in `~/.hermes/logs/`:

```
INFO: Code Assist 429, retrying in 44.0s (attempt 1/3)    # non-streaming
INFO: Code Assist stream 429, retrying in 44s (1/3)       # streaming
```

If you see `attempt 3/3` followed by an error, you've exhausted retries — the quota is likely exceeded for an extended period.

### claw-code logs

The Rust implementation logs at `info` level. Look for retry messages in stderr or the configured log output.

### Counting retries per session

If you want to know how often you're hitting rate limits, grep your session logs:

```bash
grep -c "429" ~/.hermes/logs/hermes.log
```

If this number is consistently high (>10 per session), consider:
- Switching to a model with higher RPM quota (Flash > Pro)
- Reducing `agent.max_turns` in config.yaml
- Spacing out automated sessions
