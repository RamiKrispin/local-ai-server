# Local AI Server

> **Status: Work in Progress.** This is an experimental project. The
> architecture, configuration, API behavior, and repository layout may change
> as the implementation evolves.

Local AI Server is a home AI gateway for running OpenAI-compatible clients
against local model backends. The gateway exposes `/v1` endpoints that look
like the OpenAI API, then routes requests to local services such as Ollama,
MLX, or Docker Model Runner.

The current implementation is at v0.2.0. Ollama is wired end-to-end, while
MLX and Docker Model Runner are still placeholder adapters that return 501.
API-key authentication, structured JSON logging, `/readyz`, and hot-reload of
`config/models.yaml` are implemented.

## Architecture

![Design diagram](assets/design%20diagram.png)

This diagram captures the intended direction of the project, not a frozen
contract. The project is still experimental, and pieces of this architecture
may move, shrink, or be replaced as the gateway matures.

Today, the gateway runs as a local FastAPI process under uvicorn and talks to
a host-native Ollama server. The longer-term design keeps heavy model
backends on the host while containerizing the gateway and Caddy reverse proxy
for LAN use.

## Current Scope

- OpenAI-compatible endpoints:
  - `GET /v1/models`
  - `POST /v1/chat/completions`
  - `POST /v1/embeddings`
- Ollama support for chat, streaming chat, embeddings, and health checks.
- API-key authentication for `/v1/*` routes.
- SQLite key store with Argon2id hashes.
- Key generation and revocation scripts.
- Structured JSON logs with `Authorization` redaction.
- Public `/healthz` and `/readyz` endpoints.
- Hot-reload of `config/models.yaml`.
- Capability gating for chat, embeddings, and tools.
- OpenAI-style error envelopes.

## Deferred Work

- Caddy, TLS, LAN binding, `compose.yaml`, and a gateway Docker image.
- Real MLX adapter implementation.
- Real Docker Model Runner adapter implementation.
- Host-side backend lifecycle commands.
- Per-key rate limits, quotas, expiry, and usage metering.

## Quick Start

Prerequisites:

- macOS with [`uv`](https://docs.astral.sh/uv/) installed.
- [Ollama](https://ollama.com) running on `localhost:11434`.
- Required Ollama models:

```bash
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

Install dependencies and start the gateway:

```bash
git clone https://github.com/RamiKrispin/local-ai-server
cd local-ai-server
uv sync

mkdir -p data
KEY=$(uv run python scripts/generate_api_key.py --name local-dev | grep -oE 'sk-local-[A-Za-z0-9_-]+')
echo "Save this key: $KEY"
export LOCAL_AI_KEY="$KEY"

uv run uvicorn app.main:app --port 8000
```

Verify the server:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/models
```

## Using the OpenAI Python SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key=os.environ["LOCAL_AI_KEY"],
)

response = client.chat.completions.create(
    model="ollama-llama3",
    messages=[{"role": "user", "content": "Say hi in three words."}],
)

print(response.choices[0].message.content)
```

## API Keys

Create a key:

```bash
uv run python scripts/generate_api_key.py --name <label>
```

Revoke a key by prefix:

```bash
uv run python scripts/revoke_api_key.py --prefix sk-local-AbCd
```

Keys are stored in SQLite at `KEYS_DB_PATH`, which defaults to
`./data/keys.db`. Plaintext keys are printed once and are never stored.

## Configuration

Models are registered in [`config/models.yaml`](config/models.yaml). Each
entry maps a public model id to a backend, upstream model name, and capability
list.

The gateway watches `config/models.yaml` and reloads model changes without a
restart when the backend type already exists in the running adapter set.

## Tests

```bash
uv run pytest -q
uv run pytest -q -m "not live"
uv run mypy app/ --strict
uv run ruff check .
```

Live tests require Ollama to be running with `llama3.1:8b` and
`nomic-embed-text` available.

## Documentation

- [`docs/usage-guide.md`](docs/usage-guide.md) - setup, common workflows, and troubleshooting.
- [`docs/api-reference.md`](docs/api-reference.md) - endpoint behavior and error shapes.
- [`docs/architecture.md`](docs/architecture.md) - current architecture and request lifecycle.
- [`docs/v0.1.0-scope-and-architecture.md`](docs/v0.1.0-scope-and-architecture.md) - v0.1.0 scope reference.
- [`docs/v0.2.0-scope-and-architecture.md`](docs/v0.2.0-scope-and-architecture.md) - v0.2.0 scope reference.
- [`docs/spec.md`](docs/spec.md) - longer-term v1 design.

## Roadmap

| Version | Status | Scope |
|---|---|---|
| v0.1.0 | shipped | Ollama wired, MLX and DMR stubbed, no auth. |
| v0.2.0 | shipped | API-key auth, structured logging, `/readyz`, hot-reload. |
| v0.3.0 | planned | Caddy, TLS, LAN binding, Compose, gateway image. |
| v0.4.0+ | planned | Real MLX and Docker Model Runner adapters. |

## License

[CC-BY-NC-SA-4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/).
