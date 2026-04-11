# Cloud Code Assist API — Wire Protocol Reference

This document describes the request/response shape for `cloudcode-pa.googleapis.com`, the API surface that Google's official `gemini` CLI uses. Distinct from `generativelanguage.googleapis.com` (OpenAI-compat, API key only) and `aiplatform.googleapis.com` (Vertex AI, gcloud OAuth + billed project).

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1internal:streamGenerateContent?alt=sse` | SSE streaming completion |
| `POST /v1internal:generateContent` | Non-streaming completion |
| `POST /v1internal:loadCodeAssist` | Discover an existing Code Assist project |
| `POST /v1internal:onboardUser` | Provision a new free-tier project (returns LRO) |
| `GET /v1internal/{operationName}` | Poll a long-running operation |

## Required headers

```
Authorization: Bearer <ya29 OAuth access token>
Content-Type: application/json
User-Agent: google-cloud-sdk vscode_cloudshelleditor/0.1
X-Goog-Api-Client: gl-node/22.17.0
Client-Metadata: {"ideType":"IDE_UNSPECIFIED","platform":"PLATFORM_UNSPECIFIED","pluginType":"GEMINI"}
```

Project discovery (`loadCodeAssist` / `onboardUser`) uses a slightly different `User-Agent`:

```
User-Agent: google-api-nodejs-client/9.15.1
```

These match what the official `gemini` CLI sends. Custom values may be rejected.

## Request envelope

```json
{
  "project": "gen-lang-client-XXXXXXXXXX",
  "model": "gemini-3-flash-preview",
  "userAgent": "hermes-agent",
  "requestId": "hermes-1775847499017-abc123def",
  "request": {
    "contents": [
      {
        "role": "user",
        "parts": [{"text": "..."}]
      },
      {
        "role": "model",
        "parts": [
          {
            "functionCall": {"name": "...", "args": {...}},
            "thoughtSignature": "..."
          }
        ]
      },
      {
        "role": "user",
        "parts": [
          {
            "functionResponse": {
              "name": "...",
              "response": {"output": "..."}
            }
          }
        ]
      }
    ],
    "systemInstruction": {
      "parts": [{"text": "..."}]
    },
    "generationConfig": {
      "temperature": 0.7,
      "topP": 0.95,
      "maxOutputTokens": 4096,
      "thinkingConfig": {
        "includeThoughts": true,
        "thinkingLevel": "HIGH"
      }
    },
    "tools": [
      {
        "functionDeclarations": [
          {
            "name": "...",
            "description": "...",
            "parametersJsonSchema": {...}
          }
        ]
      }
    ],
    "toolConfig": {
      "functionCallingConfig": {"mode": "AUTO"}
    }
  }
}
```

## Response envelope (SSE stream)

Each `data:` line carries:

```json
{
  "response": {
    "candidates": [
      {
        "content": {
          "parts": [
            {"text": "..."},
            {"thought": true, "text": "..."},
            {"functionCall": {"name": "...", "args": {...}}},
            {"thoughtSignature": "..."}
          ]
        },
        "finishReason": "STOP"
      }
    ],
    "usageMetadata": {
      "promptTokenCount": 123,
      "candidatesTokenCount": 456,
      "totalTokenCount": 579
    }
  }
}
```

**Critical:** `thoughtSignature` can appear on **any** part type — text, thinking, functionCall. Per Google's docs: "For non-functionCall responses, the signature appears on the last part for context replay." Track the latest non-empty signature seen across all parts and let any sibling functionCall claim it on replay.

## Project provisioning flow

```
1. POST /v1internal:loadCodeAssist
   body: {
     "cloudaicompanionProject": null,
     "metadata": {
       "ideType": "IDE_UNSPECIFIED",
       "platform": "PLATFORM_UNSPECIFIED",
       "pluginType": "GEMINI",
       "duetProject": null
     }
   }

2. If response has "currentTier" and "cloudaicompanionProject" → use it

3. Otherwise:
   POST /v1internal:onboardUser
   body: {
     "tierId": "free-tier",
     "metadata": { ... same as above ... }
   }
   → returns {"name": "operations/...", "done": false}

4. Poll: GET /v1internal/{operationName} every 5s
   → eventually {"done": true, "response": {"cloudaicompanionProject": "gen-lang-client-..."}}

5. Cache the project_id in ~/.gemini/oauth_creds.json under "project_id"
```

The hermes implementation lives in `agent/google_codeassist_project.py`.

## OAuth refresh

```
POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token
refresh_token=<from oauth_creds.json>
client_id=<GOOGLE_CLIENT_ID>
client_secret=<GOOGLE_CLIENT_SECRET>
```

Response:

```json
{
  "access_token": "ya29...",
  "expires_in": 3599,
  "scope": "...",
  "token_type": "Bearer"
}
```

Write the refreshed `access_token` + new `expiry_date` (now + `expires_in * 1000`) back to `~/.gemini/oauth_creds.json`. The refresh token doesn't change.

This is what `_refresh_gemini_cli_tokens()` in `hermes_cli/auth.py` does.

## Free-tier limits (as of 2026-04)

| Model | RPM | Notes |
|---|---|---|
| `gemini-3-flash-preview` | ~5 | Very limiting in tool-heavy workflows |
| `gemini-2.5-flash` | ~10 | Higher cap, slightly worse at tool calling |
| `gemini-3-pro` | ~3 | Slow + low quota |

Quota errors return `429` with a body like:

```json
{
  "error": {
    "code": 429,
    "status": "RESOURCE_EXHAUSTED",
    "details": [
      {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "44s"}
    ]
  }
}
```

The `GoogleCodeAssistClient` should parse `retryDelay` from the response body and sleep that long before retrying. If you see retry storms, check that the inner retry actually waits the full quota window.
