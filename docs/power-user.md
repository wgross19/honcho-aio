# Power User Guide

Advanced configuration for the honcho-aio container.

## Model strings

`EMBEDDING_MODEL` and `CHAT_MODEL` use `<provider>/<model>`. The split is on the **first** `/`. Nested slashes stay in the model name (`openrouter/openai/gpt-5.4-mini` → provider `openrouter`, model `openai/gpt-5.4-mini`).

| Provider     | Transport | Base URL (override)            | Key env                                                    |
| ------------ | --------- | ------------------------------ | ---------------------------------------------------------- |
| `local`      | openai    | `LOCAL_BASE_URL` (required)    | `LOCAL_API_KEY` (opt.; placeholder substituted when empty) |
| `ollama`     | openai    | `https://ollama.com/v1`        | `OLLAMA_API_KEY`                                           |
| `openai`     | openai    | upstream default               | `LLM_OPENAI_API_KEY`                                       |
| `openrouter` | openai    | `https://openrouter.ai/api/v1` | `LLM_OPENAI_API_KEY`                                       |
| `together`   | openai    | `https://api.together.xyz/v1`  | `LLM_OPENAI_API_KEY`                                       |
| `anthropic`  | anthropic | upstream default               | `LLM_ANTHROPIC_API_KEY`                                    |
| `gemini`     | gemini    | upstream default               | `LLM_GEMINI_API_KEY`                                       |

`EMBEDDING_MODEL` supports: `local`, `ollama`, `openai`, `openrouter`, `together`, `gemini`.
`CHAT_MODEL` supports all seven providers.

`local/` chat uses a single engine at `LOCAL_BASE_URL` with **no cloud fallback**.

`LOCAL_API_KEY` is optional for endpoints without auth. When it is empty the container substitutes the placeholder `not-needed`. Upstream Honcho requires a non-None api_key for openai-transport models, so an empty key breaks model loading. Most local OpenAI-compatible endpoints (Ollama, llama.cpp, vLLM) accept any value.

## Dimension guard

At boot the image probes the embedding model for its output width. If that width does not match `EMBEDDING_VECTOR_DIMENSIONS` (default 768) boot fails. The probe tolerates auth and connection failures (for example a local provider that is not up yet). Honcho still checks width at first request.

## Tool calling

The chat / deriver / analyst model **must support tool/function calling**. Pure-chat models break derivation and dialectic loops.

## Hermes

This image is a native Honcho memory backend, not an MCP server.

```bash
hermes memory setup
# base URL: http://<lan>:8000
hermes memory status
```

Confirm `honcho_search` / `honcho_context` / `honcho_conclude` respond after first boot.

## Listen addresses

- `8000` — Honcho API, all interfaces
- `5432` — Postgres, `127.0.0.1` only
- `6379` — Redis, `127.0.0.1` only

Do not publish Postgres or Redis. Do not give agents `DATABASE_URL`.

## Persistence

Host defaults:

- `/mnt/user/appdata/honcho-aio/postgres` → `/data/postgres`
- `/mnt/user/appdata/honcho-aio/redis` → `/data/redis`
- `/mnt/user/appdata/honcho-aio/honcho` → `/var/lib/honcho` (`runtime.env` mode 600)

## Advanced toggles

Unraid XML advanced fields: `AUTH_USE_AUTH`, `AUTH_JWT_SECRET`, `METRICS_ENABLED`, `TELEMETRY_ENABLED`, `SENTRY_ENABLED`, `SENTRY_DSN`, `WEBHOOK_SECRET`, `DERIVER_WORKERS`, `LLM_LOG_LEVEL`, `NAMESPACE`.
