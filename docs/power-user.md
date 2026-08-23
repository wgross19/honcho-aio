# Customization Guide

Use this file as the per-app advanced configuration guide in derived repos.

Recommended sections:

## 1. Internal vs External Services

Document which databases, search backends, or sidecars are internal by default and which can be overridden.

## 2. AI Provider Overrides

Model strings use the format `<provider>/<model>` where `<provider>` is one of:
`local`, `ollama`, `openai`, `openrouter`, `together`, `anthropic`, `gemini`.
The split is on the **first** `/`; the model portion may contain nested slashes
(e.g. `openrouter/openai/gpt-5.4-mini` → provider=`openrouter`, model=`openai/gpt-5.4-mini`).

### Provider → transport mapping

| Provider     | Transport | Base URL (override)            | Key env                 |
| ------------ | --------- | ------------------------------ | ----------------------- |
| `local`      | openai    | `LOCAL_BASE_URL` (required)    | `LOCAL_API_KEY` (opt.)  |
| `ollama`     | openai    | `https://ollama.com/v1`        | `OLLAMA_API_KEY`        |
| `openai`     | openai    | upstream default               | `LLM_OPENAI_API_KEY`    |
| `openrouter` | openai    | `https://openrouter.ai/api/v1` | `LLM_OPENAI_API_KEY`    |
| `together`   | openai    | `https://api.together.xyz/v1`  | `LLM_OPENAI_API_KEY`    |
| `anthropic`  | anthropic | upstream default               | `LLM_ANTHROPIC_API_KEY` |
| `gemini`     | gemini    | upstream default               | `LLM_GEMINI_API_KEY`    |

`EMBEDDING_MODEL` supports: `local`, `ollama`, `openai`, `openrouter`, `together`, `gemini`.
`CHAT_MODEL` supports all seven providers above.

`local/` chat model uses a single local engine at `LOCAL_BASE_URL` with **no cloud
fallback** — traffic is routed exclusively to the local endpoint.

### Dimension guard

At boot time the image probes the embedding model for its actual output
dimension. If the returned width does not match `EMBEDDING_VECTOR_DIMENSIONS`
(default 768) the boot fails with a clear error. The probe tolerates auth
and connection failures (e.g. local provider not yet running); Honcho's
runtime check catches dimension mismatches at first request.

### Tool-calling requirement (PRD Req 9)

The chat/deriver/analyst model **must support tool/function calling**.
Providers and models that do not support native tool calling (e.g. some
pure-chat completion models) will break the agent derivation and dialectic
loops. When selecting a `CHAT_MODEL`, verify it advertises tool-calling
capability with the target provider.

## 3. Remote Access

Document required hostname, proxy, CSRF, and trusted-domain settings.

## 4. Storage Paths

Document every mapped path and what data it stores.

## 5. Optional Integrations

Document sandboxes, web search, telemetry, TTS, auth providers, or any other optional upstream features.
