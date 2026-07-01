# Local AI Server

> **Status: Work in Progress.** This project is under active development — the spec is finalized but the implementation is still being built out. Interfaces, configuration, and layout may change.

A home AI gateway running on a Mac Studio that exposes an OpenAI-compatible HTTP API and routes requests to local LLM backends. v0.1.0 was the first vertical slice (Ollama wired). v0.2.0 adds API-key authentication, structured JSON logging, `/readyz` per-backend health, and hot-reload of `models.yaml`.

## What's in v0.2.0

- **API-key authentication** — every `/v1/*` route now requires `Authorization: Bearer sk-local-...`. `/healthz`, `/readyz`, `/docs`, `/openapi.json`, `/redoc` remain public.
- **Argon2id key store** — keys are stored in SQLite at `KEYS_DB_PATH` (default `./data/keys.db`); plaintexts are never persisted.
- **Mint and revoke CLIs** — `python scripts/generate_api_key.py --name <name>` prints the key once; `python scripts/revoke_api_key.py --prefix <prefix>` revokes it.
- **Structured JSON logging** — every gateway log line on stdout is parseable JSON with `ts`, `level`, `event`, and per-request fields (`key_prefix`, `model`, `backend`, `stream`, `status`, `latency_ms`, `prompt_tokens`, `completion_tokens` when available).
- **`Authorization` redaction** — global redaction processor scrubs any value starting with `Bearer ` to `<redacted: sk-local-XXX>`; full tokens never appear in logs.
- **`/readyz` per-backend health** — public endpoint that fans out over every backend's `health()`; 200 if any backend reachable, 503 with the OpenAI envelope only if every backend is down.
- **Hot-reload of `config/models.yaml`** — `watchfiles` watches the file; on save, the registry is atomically swapped without restarting the gateway.
- Everything from v0.1.0 (Ollama wired, MLX + DMR stubbed at 501, capability/tools gating) is unchanged on the wire.

## What's deferred

- Caddy + TLS + LAN binding + `compose.yaml` — v0.3.0.
- Real MLX and Docker Model Runner backends — v0.4.0+.
- Per-key rate limits, quotas, expiry — post-v1.
- See [`docs/spec.md`](docs/spec.md) for the full v1 design.

## Architecture

![Design diagram](assets/design%20diagram.png)

v0.2.0 adds an ASGI auth middleware, structlog JSON logging with Authorization redaction, a watchfiles-driven registry hot-reloader, and a `/readyz` endpoint that fans out over `app.state.adapters[*].health()`. The gateway and Caddy reverse proxy will run in containers (v0.3.0+); the three model backends run natively on the host. v0.2.0 still runs the gateway as a plain host process under uvicorn against a host-native Ollama.

## Prerequisites

- macOS with [`uv`](https://docs.astral.sh/uv/) installed.
- [Ollama](https://ollama.com) installed and `ollama serve` running on `localhost:11434`.
- Required Ollama models pulled:

```bash
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

## Quick start

```bash
git clone https://github.com/RamiKrispin/local-ai-server
cd local-ai-server
uv sync
cp config/.env.example config/.env  # optional; defaults work

# Mint your first API key (the plaintext is printed once — save it).
mkdir -p data
KEY=$(uv run python scripts/generate_api_key.py --name claude-code | grep -oE 'sk-local-[A-Za-z0-9_-]+')
echo "Save this key: $KEY"

# Start the gateway.
uv run uvicorn app.main:app --port 8000
```

Verify the server is up:

```bash
curl http://127.0.0.1:8000/healthz             # {"status":"ok"}
curl http://127.0.0.1:8000/readyz              # {"status":"ok","backends":{...}}
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/models | jq .
```

## Auth: minting and using API keys

### The key format

Keys are `sk-local-` followed by 32 URL-safe random bytes. The first 12 characters are the SQL primary key (the *prefix*); the full plaintext is never stored.

### Minting

```bash
mkdir -p data
uv run python scripts/generate_api_key.py --name <human-readable-label>
# Prints: sk-local-<token>
# The plaintext is printed exactly once. Save it — it cannot be recovered.
```

### Using the key

Include the key as a bearer token on every `/v1/*` request:

```
Authorization: Bearer sk-local-...
```

### The 401 envelope

```json
{"error": {"type": "invalid_request_error", "message": "Invalid API key", "param": "Authorization", "code": "invalid_api_key"}}
```

### Revocation

```bash
# The prefix is the first 12 characters of the key (e.g. sk-local-AbCd).
uv run python scripts/revoke_api_key.py --prefix sk-local-AbCd
```

### Where keys are stored

`KEYS_DB_PATH` (default `./data/keys.db`) — gitignored SQLite file, no separate service required. The script creates the file and table on first run.

### Last-used tracking

Every successful request updates `last_used_at` in the row. Inspect via:

```bash
sqlite3 data/keys.db "SELECT prefix, name, datetime(last_used_at, 'unixepoch') FROM api_keys"
```

### What's NOT done

Per-key rate limits, quotas, expiry — deferred to post-v1.

## Using the OpenAI Python SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key=os.environ["LOCAL_AI_KEY"],  # export LOCAL_AI_KEY=sk-local-...
)

# List models
print([m.id for m in client.models.list().data])

# Non-streaming chat
r = client.chat.completions.create(
    model="ollama-llama3",
    messages=[{"role": "user", "content": "Say hi in three words."}],
)
print(r.choices[0].message.content)

# Streaming chat
for chunk in client.chat.completions.create(
    model="ollama-llama3",
    messages=[{"role": "user", "content": "Stream a haiku."}],
    stream=True,
):
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()

# Embeddings
e = client.embeddings.create(model="ollama-nomic-embed", input="hello")
print("dim:", len(e.data[0].embedding))
```

## Configuration: `config/models.yaml`

```yaml
models:
  - id: ollama-llama3
    backend: ollama
    upstream_model: llama3.1:8b
    capabilities: [chat, tools]
  - id: ollama-nomic-embed
    backend: ollama
    upstream_model: nomic-embed-text
    capabilities: [embeddings]
  - id: mlx-mistral             # stubbed at 501 in v0.1.0
    backend: mlx
    upstream_model: mlx-community/Mistral-7B-Instruct-v0.3
    base_url: http://localhost:8080
    capabilities: [chat]
  - id: model-runner-llama32    # stubbed at 501 in v0.1.0
    backend: docker_model_runner
    upstream_model: ai/llama3.2
    capabilities: [chat, embeddings, tools]
```

**Hot-reload**: edit and save `config/models.yaml` while the gateway is running — changes appear in `/v1/models` within ~1 s without restarting the process. See [Hot-reload of models.yaml](#hot-reload-of-modelssyaml) below.

## `/readyz`

Public endpoint (no auth required) that fans out over every adapter's `health()` concurrently.

**200 response (any backend up):**

```json
{
  "status": "ok",
  "backends": {
    "ollama": {"status": "ok", "models": ["llama3.1:8b", "nomic-embed-text"]},
    "mlx": {"status": "error", "error": "NotSupportedError: MLX adapter is not implemented in v0.1.0"},
    "docker_model_runner": {"status": "error", "error": "NotSupportedError: ..."}
  }
}
```

**503 response (all backends unreachable):**

```json
{
  "error": {
    "type": "service_unavailable",
    "message": "No backends reachable",
    "param": null,
    "code": "no_backends_reachable"
  },
  "backends": {
    "ollama": {"status": "unreachable", "error": "..."},
    "mlx": {"status": "error", "error": "NotSupportedError: ..."},
    "docker_model_runner": {"status": "error", "error": "NotSupportedError: ..."}
  }
}
```

The response is binary: 200 if any backend is reachable, 503 only if none are. There is no `"degraded"` intermediate state.

## Hot-reload of `models.yaml`

`watchfiles` watches `MODELS_YAML_PATH`; on every save, the registry is reloaded atomically.

**Atomic-swap discipline**: `app.state.registry = new_registry` is one Python operation; in-flight requests complete against the registry snapshot they entered with. Subsequent requests see the new registry.

**What works**: adding, editing, or removing a model that targets an existing backend (`ollama`, `mlx`, `docker_model_runner`).

**v0.2.0 limitation**: adding a model with a *new* backend type (e.g. the first-ever `mlx` entry when no MLX models existed at startup) requires a process restart, because adapters are constructed at lifespan startup and not rebuilt on hot-reload.

**Failure handling**: if the new YAML is malformed, the watcher logs `event="registry_reload_failed"` and keeps the previous registry live — the gateway never goes down due to a bad YAML save.

## Tests

```bash
uv run pytest -q              # all tests; live tests auto-skip if Ollama isn't running
uv run pytest -q -m "not live" # explicitly skip live tests
uv run mypy app/ --strict     # type-check
uv run ruff check .
```

Notes:
- Auth tests use a temp SQLite key store (no real keys touched).
- Live tests require `ollama serve` with both `llama3.1:8b` and `nomic-embed-text` pulled.
- Running `pytest -n auto` (xdist) is not recommended — live tests may flake under parallel execution.

## Demo notebooks

- [`posts/v0_2_0_auth_demo.ipynb`](posts/v0_2_0_auth_demo.ipynb) — auth flow: key mint + authenticated request + 401 path.
- [`posts/v0_2_0_readyz_hotreload_demo.ipynb`](posts/v0_2_0_readyz_hotreload_demo.ipynb) — `/readyz` probing + hot-reload demonstration.
- [`examples/v0_1_0_demo.ipynb`](examples/v0_1_0_demo.ipynb) — v0.1.0 end-to-end demo (list models, chat, streaming, embeddings, 501 stub).

## Version roadmap

| Version | Status | Scope |
|---|---|---|
| v0.1.0 | shipped (2026-06-18) | Ollama wired; MLX + DMR stubbed at 501; no auth; host process. |
| **v0.2.0** | **shipped (2026-06-19)** | **API-key auth (Argon2id + SQLite); structlog JSON logging; watchfiles hot-reload; `/readyz`.** |
| v0.3.0 | planned | Caddy + TLS + LAN binding; `compose.yaml`; `Dockerfile.gateway`. |
| v0.4.0+ | planned | Real MLX adapter; real Docker Model Runner adapter; `Makefile` for host-backend lifecycle. |

## Documentation

The full v1 design is in [`docs/spec.md`](docs/spec.md), including the request lifecycle, repository layout, adapter contracts, configuration schema, auth flow, container/Caddy setup, host-side Makefile for backend lifecycle, and the end-to-end verification plan.

## Reference docs

Supplementary docs grounded in the v0.1.0 implementation:

- [`docs/api-reference.md`](docs/api-reference.md) — endpoints, request fields, error envelope, capability/tools gating decision table, backend matrix.
- [`docs/usage-guide.md`](docs/usage-guide.md) — setup walkthrough, common tasks (list models, chat, streaming, embeddings, adding a model), and troubleshooting.
- [`docs/architecture.md`](docs/architecture.md) — components, request lifecycle, lifespan, streaming contract, and v0.2.0+ extension points.
- [`docs/spec.md`](docs/spec.md) — long-term v1 design (auth, Caddy, TLS, host Makefile, end-to-end verification).

## License

[CC-BY-NC-SA-4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
