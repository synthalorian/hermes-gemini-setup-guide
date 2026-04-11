"""
agent/google_codeassist_protocol.py — annotated sketch.

This is a SKETCH of the protocol translator + signature cache. The full
implementation in hermes is ~800 lines and handles SSE parsing, project
discovery, retry-on-429, and the OpenAI ↔ Code Assist envelope translation.

This sketch shows the load-bearing pieces:
- The MODULE-LEVEL signature cache (NOT per-instance)
- The latest-signature-tracking pattern in the stream parser
- The lookup + attach pattern in the request translator
"""

import json
import threading
from typing import Any


# =============================================================================
# Module-level thoughtSignature cache (THE thinking-mode replay fix)
# =============================================================================

# CRITICAL: this MUST be module-level, not per-instance.
#
# Hermes' run_agent.py calls _create_request_openai_client(shared=False) for
# every API request. A per-instance cache will be empty on every replay,
# leading to a 400 from Code Assist on the second tool call:
#
#   400 Bad Request: function call X in the N. content block is missing a thought_signature
#
# Putting the cache at module scope means it survives across requests within
# the same process, which is what we need.

_SIGNATURE_CACHE: dict[str, str] = {}
_SIGNATURE_CACHE_LOCK = threading.Lock()


def capture_signature(tool_call_id: str, name: str, args: dict, signature: str) -> None:
    """Cache a thoughtSignature seen in a response.

    Cache by both tool_call_id (primary) and a (name, canonical_args) payload
    fallback in case some agent layer rewrites tool_call_ids between turns.
    """
    canonical_args = json.dumps(args, sort_keys=True)
    payload_key = f"{name}::{canonical_args}"
    with _SIGNATURE_CACHE_LOCK:
        _SIGNATURE_CACHE[tool_call_id] = signature
        _SIGNATURE_CACHE[payload_key] = signature


def lookup_signature(tool_call_id: str, name: str, args: dict) -> str | None:
    """Look up a cached thoughtSignature when replaying an assistant tool call."""
    canonical_args = json.dumps(args, sort_keys=True)
    payload_key = f"{name}::{canonical_args}"
    with _SIGNATURE_CACHE_LOCK:
        sig = _SIGNATURE_CACHE.get(tool_call_id)
        if sig:
            return sig
        return _SIGNATURE_CACHE.get(payload_key)


# =============================================================================
# Stream parser — tracks the latest signature across ALL parts in a response
# =============================================================================

class StreamState:
    """Per-request stream parser state.

    The latest_signature field tracks the most recently seen thoughtSignature
    across ALL parts in the current response. This is necessary because
    Google's docs say: "For non-functionCall responses, the signature appears
    on the last part for context replay." So a functionCall part on line 3
    might have its signature on a separate part on line 4.
    """

    def __init__(self) -> None:
        self.latest_signature: str | None = None

    def observe_part(self, part: dict[str, Any]) -> None:
        """Update latest_signature if this part carries one."""
        sig = part.get("thoughtSignature")
        if sig:
            self.latest_signature = sig

    def claim_signature_for_function_call(
        self, part: dict[str, Any], tool_call_id: str, name: str, args: dict
    ) -> None:
        """Cache the appropriate signature for a functionCall part.

        Order of preference:
        1. The signature on this part itself (if present)
        2. The latest signature seen so far across all parts
        """
        signature = part.get("thoughtSignature") or self.latest_signature
        if signature:
            capture_signature(tool_call_id, name, args, signature)


# =============================================================================
# Response translator — Code Assist → OpenAI shape
# =============================================================================

def parse_code_assist_response_into_openai_message(
    response: dict[str, Any],
) -> dict[str, Any]:
    """Walk the Code Assist response and produce an OpenAI-shaped message.

    Side effect: populates the module-level signature cache.
    """
    state = StreamState()
    candidate = response["candidates"][0]
    parts = candidate["content"]["parts"]

    text_chunks: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    # FIRST pass: observe ALL parts to find the latest signature
    for part in parts:
        state.observe_part(part)

    # SECOND pass: process parts and let functionCalls claim signatures
    for part in parts:
        if "text" in part and not part.get("thought"):
            text_chunks.append(part["text"])
        elif "functionCall" in part:
            fc = part["functionCall"]
            name = fc["name"]
            args = fc.get("args", {})
            # Generate a tool_call_id (or use one from the response if present)
            tool_call_id = f"call_{len(tool_calls)}_{name}"
            tool_calls.append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }
            )
            # Cache the signature for this tool call
            state.claim_signature_for_function_call(part, tool_call_id, name, args)

    return {
        "role": "assistant",
        "content": "".join(text_chunks) if text_chunks else None,
        "tool_calls": tool_calls or None,
    }


# =============================================================================
# Request translator — OpenAI → Code Assist shape
# =============================================================================

def translate_assistant_message_to_codeassist(message: dict[str, Any]) -> dict[str, Any]:
    """Convert an OpenAI assistant message into Code Assist format.

    For each tool_call, look up the cached thoughtSignature and attach it
    to the functionCall part. This is the read side of the cache.
    """
    parts: list[dict[str, Any]] = []

    if message.get("content"):
        parts.append({"text": message["content"]})

    for tool_call in message.get("tool_calls") or []:
        name = tool_call["function"]["name"]
        args = json.loads(tool_call["function"]["arguments"])

        signature = lookup_signature(tool_call["id"], name, args)

        part: dict[str, Any] = {
            "functionCall": {
                "name": name,
                "args": args,
            }
        }
        if signature:
            part["thoughtSignature"] = signature
        parts.append(part)

    return {"role": "model", "parts": parts}
