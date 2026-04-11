"""
agent/google_codeassist_client.py — annotated sketch.

This is a SKETCH of the Code Assist client. The full implementation in hermes
is ~630 lines and provides a drop-in `openai.OpenAI`-shaped interface.

What this sketch leaves out (see the real file for the complete picture):
- Full SSE streaming consumer
- Project discovery integration (calls into google_codeassist_project.py)
- Retry-on-429 with retryDelay parsing from the response body
- Token usage tracking
- Quota detection + waiting

The point of this sketch is to show the SHAPE of the wrapper, so hermes
can swap it in for `openai.OpenAI` whenever the provider is `gemini-oauth`.
"""

import json
import time
from typing import Any, Iterator

import requests

from agent.google_codeassist_protocol import (
    parse_code_assist_response_into_openai_message,
    translate_assistant_message_to_codeassist,
)
from agent.google_codeassist_project import discover_project_id


# =============================================================================
# Constants
# =============================================================================

CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"

REQUIRED_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "google-cloud-sdk vscode_cloudshelleditor/0.1",
    "X-Goog-Api-Client": "gl-node/22.17.0",
    "Client-Metadata": (
        '{"ideType":"IDE_UNSPECIFIED","platform":"PLATFORM_UNSPECIFIED",'
        '"pluginType":"GEMINI"}'
    ),
}


# =============================================================================
# OpenAI-shaped wrapper
# =============================================================================

class _ChatCompletions:
    """Mimics openai.OpenAI().chat.completions surface."""

    def __init__(self, parent: "GoogleCodeAssistClient") -> None:
        self._parent = parent

    def create(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        """OpenAI-shaped completion call. Returns either a single response
        or an iterator depending on stream=."""
        request_body = self._parent._build_request_body(
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        if stream:
            return self._parent._stream_generate_content(request_body)
        return self._parent._generate_content(request_body)


class _Chat:
    def __init__(self, parent: "GoogleCodeAssistClient") -> None:
        self.completions = _ChatCompletions(parent)


class GoogleCodeAssistClient:
    """Drop-in replacement for openai.OpenAI(...) when provider is gemini-oauth.

    Construct it with the access_token from resolve_gemini_runtime_credentials():

        creds = resolve_gemini_runtime_credentials()
        client = GoogleCodeAssistClient(api_key=creds["api_key"])
        resp = client.chat.completions.create(
            model="gemini-3-flash-preview",
            messages=[...],
            tools=[...],
        )
    """

    def __init__(self, api_key: str, base_url: str = CODE_ASSIST_ENDPOINT) -> None:
        self._access_token = api_key
        self._base_url = base_url
        self._project_id: str | None = None
        self.chat = _Chat(self)

    def _ensure_project(self) -> str:
        if self._project_id is None:
            self._project_id = discover_project_id(self._access_token)
        return self._project_id

    def _headers(self) -> dict[str, str]:
        return {
            **REQUIRED_HEADERS,
            "Authorization": f"Bearer {self._access_token}",
        }

    def _build_request_body(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """Translate OpenAI-shaped messages into a Code Assist request body."""
        contents: list[dict[str, Any]] = []
        system_text: str | None = None

        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            elif msg["role"] == "assistant":
                contents.append(translate_assistant_message_to_codeassist(msg))
            elif msg["role"] == "user":
                contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
            elif msg["role"] == "tool":
                # Tool result — wrap as a user message with functionResponse part
                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "name": msg.get("name", "tool"),
                            "response": {"output": msg["content"]},
                        }
                    }]
                })

        inner: dict[str, Any] = {"contents": contents}

        if system_text:
            inner["systemInstruction"] = {"parts": [{"text": system_text}]}

        generation_config: dict[str, Any] = {}
        if temperature is not None:
            generation_config["temperature"] = temperature
        if max_tokens is not None:
            generation_config["maxOutputTokens"] = max_tokens
        if generation_config:
            inner["generationConfig"] = generation_config

        if tools:
            inner["tools"] = [{
                "functionDeclarations": [
                    {
                        "name": t["function"]["name"],
                        "description": t["function"].get("description", ""),
                        "parametersJsonSchema": t["function"].get("parameters", {}),
                    }
                    for t in tools
                ]
            }]
            inner["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        project_id = self._ensure_project()
        return {
            "project": project_id,
            "model": model,
            "userAgent": "hermes-agent",
            "requestId": f"hermes-{int(time.time() * 1000)}-{id(self)}",
            "request": inner,
        }

    def _generate_content(self, body: dict[str, Any]) -> Any:
        """Non-streaming POST."""
        url = f"{self._base_url}/v1internal:generateContent"
        response = requests.post(
            url, headers=self._headers(), json=body, timeout=600
        )
        if response.status_code == 429:
            self._handle_quota_error(response)
            return self._generate_content(body)  # retry once after sleeping
        response.raise_for_status()
        return self._wrap_openai_response(response.json())

    def _stream_generate_content(self, body: dict[str, Any]) -> Iterator[Any]:
        """SSE streaming POST. The full implementation parses each `data:` line
        and yields ChatCompletionChunk-shaped objects."""
        url = f"{self._base_url}/v1internal:streamGenerateContent?alt=sse"
        response = requests.post(
            url, headers=self._headers(), json=body, stream=True, timeout=600
        )
        if response.status_code == 429:
            self._handle_quota_error(response)
            yield from self._stream_generate_content(body)
            return
        response.raise_for_status()
        # ... parse SSE and yield chunks ... (see real implementation)
        yield from ()

    def _handle_quota_error(self, response: requests.Response) -> None:
        """Parse retryDelay from the 429 body and sleep."""
        try:
            err = response.json().get("error", {})
            for detail in err.get("details", []):
                retry_delay = detail.get("retryDelay")
                if retry_delay and retry_delay.endswith("s"):
                    seconds = int(retry_delay.rstrip("s"))
                    time.sleep(seconds + 1)
                    return
        except Exception:
            pass
        time.sleep(30)  # fallback

    def _wrap_openai_response(self, body: dict[str, Any]) -> Any:
        """Translate the Code Assist response into an OpenAI-shaped object."""
        message = parse_code_assist_response_into_openai_message(body["response"])
        # In the real implementation this is a proper ChatCompletion / ChatCompletionMessage
        # dataclass. Here we just return a plain dict for the sketch.
        return {
            "id": "chatcmpl-codeassist",
            "object": "chat.completion",
            "model": body.get("model", "gemini-3-flash-preview"),
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": body["response"].get("usageMetadata", {}),
        }
