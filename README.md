# Local AI Server

> **Status: Work in Progress.** This project is under active development — the spec is finalized but the implementation is still being built out. Interfaces, configuration, and layout may change.

A home AI gateway running on a Mac Studio that exposes an OpenAI-compatible HTTP API and routes requests to local LLM backends. v0.1.0 is the first vertical slice: a FastAPI gateway wired to Ollama, with MLX and Docker Model Runner stubbed at HTTP 501 to lock the routing seam.

## What's in v0.1.0

- OpenAI-compatible HTTP surface (`GET /v1/models`, `POST /v1/chat/completions` (stream + non-stream), `POST /v1/embeddings`).
- Ollama backend wired end-to-end via async httpx with SSE streaming.
- Capability gating (`chat`, `embeddings`, `tools`) and tools-gate (HTTP 400 before backend dispatch).
- `BackendAdapter` ABC with `MLXAdapter` and `DockerModelRunnerAdapter` returning HTTP 501 on every method, locking the routing seam for v0.4.0+.
- Live integration tests against a real local Ollama; mock-based tests for the stub adapters.

## What's deferred

- API-key auth (Argon2id + SQLite) — v0.2.0. See [`docs/spec.md`](docs/spec.md).
- Caddy + TLS + LAN binding + `compose.yaml` — v0.3.0.
- Real MLX and Docker Model Runner backends — v0.4.0+.
- Hot-reload of `models.yaml` via watchfiles — v0.2.0.
- Structured JSON logging via structlog — v0.2.0.
- See [`docs/spec.md`](docs/spec.md) for the full v1 design.

## Architecture

![Design diagram](assets/design%20diagram.png)

The gateway and Caddy reverse proxy will run in containers (v0.3.0+); the three model backends run natively on the host (Apple Metal has no GPU passthrough into Docker on Mac, and MLX has no Linux wheels). v0.1.0 runs the gateway as a plain host process under uvicorn against a host-native Ollama.

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
uv run uvicorn app.main:app --port 8000
```

Verify the server is up:

```bash
curl http://127.0.0.1:8000/healthz             # {"status":"ok"}
curl http://127.0.0.1:8000/v1/models | jq .
```

## Using the OpenAI Python SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-used-yet")

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

## Tests

```bash
uv run pytest -q              # all tests; live tests auto-skip if Ollama isn't running
uv run pytest -q -m "not live" # explicitly skip live tests
uv run ruff check .
```

Note: live tests require `ollama serve` with both `llama3.1:8b` and
`nomic-embed-text` pulled. Running `pytest -n auto` (xdist) is not
recommended — live tests may flake under parallel execution against a
single Ollama process.

## Demo notebook

A runnable end-to-end demo lives at [`examples/v0_1_0_demo.ipynb`](examples/v0_1_0_demo.ipynb) — list models, non-stream chat, stream chat, embeddings, and a 501 example from a stub backend.

## Version roadmap

| Version | Scope |
|---|---|
| **v0.1.0** | Ollama wired; MLX + DMR stubbed at 501; no auth; host process. |
| v0.2.0 | API-key auth (Argon2id + SQLite); structlog; watchfiles hot-reload; `/readyz`. |
| v0.3.0 | Caddy + TLS + LAN binding; `compose.yaml`; `Dockerfile.gateway`. |
| v0.4.0+ | Real MLX adapter; real Docker Model Runner adapter; `Makefile` for host-backend lifecycle. |

## Documentation

The full v1 design is in [`docs/spec.md`](docs/spec.md), including the request lifecycle, repository layout, adapter contracts, configuration schema, auth flow, container/Caddy setup, host-side Makefile for backend lifecycle, and the end-to-end verification plan.

## License

[CC-BY-NC-SA-4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
