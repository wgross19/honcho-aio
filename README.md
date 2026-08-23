# honcho-aio

Run [Honcho](https://github.com/plastic-labs/honcho) as one Unraid container. Honcho is memory for stateful agents. It stores messages, reasons in the background, then returns context, search, and peer conclusions. honcho-aio runs that stack as one image: Honcho API, PostgreSQL 17 + pgvector, Redis, and s6. Databases stay on loopback. Point Hermes at `http://<lan>:8000`. No MCP.

Install the [Unraid template](https://raw.githubusercontent.com/wgross19/awesome-unraid/main/honcho-aio.xml), set a few values, and it is ready on first boot.

## What this is for

Honcho is self-hosted agent memory. Point an AI agent harness at it and it stores, derives, and retrieves conversation context. **`honcho-aio` packages Honcho so you can run it as a hosted memory backend** for **Hermes Agent** (native `honcho_*` tools, not MCP) and any client that speaks the Honcho HTTP API. Agents connect to `http://<lan>:8000`. They never get a database URL.

## The problem it solves

Honcho will not just run. A real deployment needs four things bolted together:

1. **PostgreSQL + pgvector** — workspaces, messages, and embeddings live here
2. **Redis** — deriver queues and cache
3. **Embedding + chat models** — local Ollama or a cloud provider, with matching vector width
4. **A build** — Honcho has no official all-in-one Unraid image

Wiring those by hand on Unraid is the whole problem. `honcho-aio` does it for you.

## What you get in this container

**Honcho v3.0.12, running under s6** — the reason you are here:

- **honcho-api** on `0.0.0.0:8000` (`/health` returns `{"status":"ok"}`)
- **honcho-deriver** — background derivation and dialectic
- **First-boot migrations + workspace create**
- **Provider parse** — `EMBEDDING_MODEL` and `CHAT_MODEL` use `<provider>/<model>` (split on the first `/`)
- **Dimension guard** — boot fails if the embedding width does not match `EMBEDDING_VECTOR_DIMENSIONS`

Plus the supporting infrastructure, all in the same container:

- **PostgreSQL 17 + pgvector** — loopback only, never published
- **Redis** — loopback only, never published

Pinned to official `plastic-labs/honcho` tags (`HONCHO_VERSION` + `HONCHO_GIT_SHA`). Agents connect only through HTTP on port 8000.

## Embeddings & models

Set both models as `<provider>/<model>`. Nested slashes stay in the model name (`openrouter/openai/gpt-5.4-mini`).

- **Local (recommended on Unraid)** — `EMBEDDING_MODEL=local/embeddinggemma` and a local chat model. Set `LOCAL_BASE_URL` to your OpenAI-compat endpoint (example `http://<lan>:11434/v1`). Local chat has **no cloud fallback**.
- **Cloud** — `openai/`, `openrouter/`, `together/`, `anthropic/` (chat only), or `gemini/`. Fill only the key for the provider you picked.
- **Width** — default `EMBEDDING_VECTOR_DIMENSIONS=768` for `embeddinggemma`. A mismatch fails boot.
- **Tool calling** — the chat model must support tool/function calling. Pure-chat models break deriver loops.

## First boot does the setup for you

Start empty, and the container:

1. Refuses to start if `POSTGRES_PASSWORD` is empty (exit 64)
2. Writes `/var/lib/honcho/runtime.env` (mode `600`)
3. Starts postgres, redis, honcho-api, and honcho-deriver
4. Runs migrations
5. Probes embedding width when a key/endpoint is available

No `docker exec` required.

## Install (Unraid, ~3 min)

1. Install **honcho-aio** from Community Applications (or the [catalog XML](https://raw.githubusercontent.com/wgross19/awesome-unraid/main/honcho-aio.xml))
2. Set `POSTGRES_PASSWORD` (required)
3. Set `EMBEDDING_MODEL` and `CHAT_MODEL`. For `local/`, also set `LOCAL_BASE_URL`
4. Leave default appdata paths, Apply
5. Wait for `/health`, then point Hermes: `hermes memory setup` → `http://<lan>:8000`

## Requirements

- Unraid (with Community Applications) or any Docker host
- A LAN IP you control
- For `local/` models: an OpenAI-compat endpoint (Ollama at `http://<lan>:11434/v1` is the usual case)

## Not included

- No host PostgreSQL or Redis — both run inside the container
- No TLS / reverse proxy — Honcho listens on HTTP `:8000`
- No MCP server — Hermes uses the native Honcho memory provider
- Does not replace a live `/docker-compose/honcho` stack unless you do that yourself

## Persistence

Back up Postgres, Redis, and Honcho state under `/mnt/user/appdata/honcho-aio/` if you care about the instance.

## License

See the repo `LICENSE` if present. Honcho is under its own upstream license.
