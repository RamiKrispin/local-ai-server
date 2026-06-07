# Local AI Server — Detailed Specification

## 1. Context & Goals

The repo (`/Users/ramikrispin/Personal/tutorials/local-ai-server`) is a Python dev-container template only — no app code yet. The goal is to build a home AI server on a Mac Studio that exposes an **OpenAI-compatible HTTP API** and routes requests to multiple **local LLM backends**: Ollama, MLX (`mlx_lm.server`), and Docker Model Runner.

### Goals
- Any OpenAI SDK client (Python, Node, LangChain, etc.) works against the server with **zero code changes** — only `base_url` + `api_key` differ.
- Single LAN endpoint that fans out to whichever local backend hosts a given model.
- Containerized runtime (gateway + reverse proxy) with a clean separation between dev environment (existing template) and production runtime (new images).
- Secure for LAN use: TLS on the wire, API-key auth, no public exposure, no plaintext secrets at rest.

### Non-goals (v1)
- Public internet exposure / Let's Encrypt.
- Per-key rate limits, quotas, billing, or usage metering.
- Cross-backend tool-call normalization.
- Web UI / multi-tenant admin.
- Embeddings auto-fallback across backends.
- Legacy `/v1/completions` endpoint.

### Hard constraint that shapes the architecture
MLX is built on Apple Metal; Docker Desktop on Mac runs Linux containers in a VM with **no Metal/GPU passthrough** and MLX has no Linux wheels. Native Ollama is also significantly faster than dockerized Ollama on Mac. Therefore **all three backends run natively on the host**; only the **gateway** and **Caddy** run in containers, and they reach the host via `host.docker.internal` (Ollama, MLX) and `model-runner.docker.internal` (Docker Model Runner).

---

## 2. Architecture

```
                           ┌──────────────────────────────────────┐
   LAN client (192.168.x)  │              Mac Studio host         │
        │                  │                                      │
        │  HTTPS :443      │   ┌─────────┐    ┌──────────────┐    │
        ├─────────────────►│   │  Caddy  │──► │   Gateway    │    │
        │  (TLS internal,  │   │  (cont.)│    │   FastAPI    │    │
        │   API key reqd.) │   └─────────┘    │    (cont.)   │    │
        │                  │                  └──────┬───────┘    │
                           │                         │            │
                           │   host.docker.internal──┤            │
                           │                         │            │
                           │   ┌──────────┬──────────┴──────────┐ │
                           │   │ Ollama   │ mlx_lm.server       │ │
                           │   │ :11434   │ :8080 (1 / model)   │ │
                           │   │ (native) │ (native)            │ │
                           │   └──────────┴─────────────────────┘ │
                           │                                      │
                           │   model-runner.docker.internal       │
                           │   (Docker Desktop Model Runner)      │
                           └──────────────────────────────────────┘
```

### Components
1. **FastAPI gateway** (containerized) — auth, routing, schema validation, logging, OpenAI-compat shapes.
2. **Adapter layer** — `BackendAdapter` ABC + concrete `OllamaAdapter`, `MLXAdapter`, `DockerModelRunnerAdapter`. Thin async httpx proxies; OpenAI compat at the upstream means we mostly relay.
3. **Model registry** — YAML mapping logical model name → backend + upstream model + capabilities.
4. **Caddy reverse proxy** (containerized) — terminates TLS via `tls internal`, binds to LAN interface only.
5. **API key store** — SQLite, Argon2id hashes + visible 12-char prefix.
6. **Host-side backends** — managed via `Makefile` (Compose can't reach them).

### Request lifecycle (chat completions)
```
client → Caddy (TLS terminate) → Gateway (auth → registry lookup → adapter)
       → httpx → Ollama/MLX/Model Runner → SSE/JSON back
       → Gateway (passthrough or wrap) → Caddy → client
```

---

## 3. Repository Layout (after build-out)

```
local-ai-server/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, lifespan, router mounts
│   ├── config.py                  # pydantic-settings: env vars
│   ├── schemas.py                 # OpenAI v1 Pydantic mirrors
│   ├── auth.py                    # Bearer middleware, Argon2id verify
│   ├── registry.py                # models.yaml loader + hot-reload
│   ├── logging.py                 # structured JSON, header redaction
│   ├── errors.py                  # OpenAI-compatible error envelope
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── chat.py                # POST /v1/chat/completions
│   │   ├── embeddings.py          # POST /v1/embeddings
│   │   ├── models.py              # GET  /v1/models
│   │   └── health.py              # GET  /healthz, /readyz
│   └── adapters/
│       ├── __init__.py
│       ├── base.py                # ABC + NotSupportedError
│       ├── ollama.py
│       ├── mlx.py
│       └── docker_model_runner.py
├── config/
│   ├── models.yaml                # registry (sample)
│   └── .env.example               # gateway env vars
├── caddy/
│   └── Caddyfile
├── docker/
│   ├── Dockerfile.gateway         # NEW runtime image
│   ├── Dockerfile_Base            # existing dev container — unchanged
│   ├── Dockerfile_Dev             # existing dev container — unchanged
│   ├── build_base_docker.sh       # existing — unchanged
│   ├── build_dev_docker.sh        # existing — unchanged
│   ├── install_uv.sh              # existing — reused inside gateway image
│   ├── install_quarto.sh          # existing — unchanged
│   ├── install_dependencies.sh    # existing — unchanged
│   ├── setting_git.sh             # existing — unchanged
│   ├── requirements.txt           # existing dev deps — extend with runtime libs
│   └── .p10k.zsh                  # existing — unchanged
├── scripts/
│   ├── generate_api_key.py        # mints sk-local-... + writes hash
│   └── revoke_api_key.py
├── tests/
│   ├── conftest.py
│   ├── test_chat.py
│   ├── test_chat_streaming.py
│   ├── test_embeddings.py
│   ├── test_models_endpoint.py
│   ├── test_auth.py
│   ├── test_registry.py
│   └── adapters/
│       ├── test_ollama_adapter.py
│       ├── test_mlx_adapter.py
│       └── test_docker_model_runner_adapter.py
├── compose.yaml                   # gateway + caddy services
├── pyproject.toml                 # runtime + dev deps via uv
├── Makefile                       # host-side backend lifecycle
├── ruff.toml                      # existing — unchanged
├── README.md                      # rewrite end-to-end
├── .gitignore                     # extend with .env, .keys.db
├── .devcontainer/                 # existing — unchanged
└── .vscode/                       # existing — unchanged
```

---

## 4. API Surface

All endpoints under `/v1/`. Wire format follows OpenAI v1 verbatim so SDK clients work unmodified.

### 4.1 GET /v1/models
Returns the union of all enabled models from `models.yaml`.

**Response:**
```json
{
  "object": "list",
  "data": [
    {"id": "ollama-llama3", "object": "model", "created": 1717000000, "owned_by": "local"},
    {"id": "mlx-mistral",   "object": "model", "created": 1717000000, "owned_by": "local"}
  ]
}
```

### 4.2 POST /v1/chat/completions
Both streaming (`stream=true`) and non-streaming. Accepts standard OpenAI fields including `tools`, `tool_choice`, `response_format`, `temperature`, `max_tokens`, `top_p`, `seed`, `stop`, `messages`. Unsupported fields are dropped with a warning log; unsupported-on-this-backend fields fail with HTTP 400.

**Streaming:** SSE per OpenAI spec — events of the form `data: {json}\n\n`, terminated by `data: [DONE]\n\n`. The gateway passes the upstream stream through verbatim.

### 4.3 POST /v1/embeddings
Standard `{model, input}` → `{object: "list", data: [{embedding: [...], index, object}], model, usage}`.
- Routed only to backends with `embeddings` in `capabilities`. Routing to a backend that lacks support returns HTTP 501 with body:
```json
{"error": {"type": "not_supported", "message": "Backend 'mlx' does not support embeddings", "param": "model", "code": "backend_capability_missing"}}
```

### 4.4 GET /healthz, GET /readyz
- `/healthz` (unauthenticated): always 200 if process up.
- `/readyz` (unauthenticated): pings each adapter's `health()`. Returns 200 if **any** backend is reachable; payload lists per-backend status. The gateway degrades per-model rather than failing closed.

### 4.5 Tool / function calling
Pass-through. The registry declares `supports_tools: true|false` per model. If `tools` is in the request and the model is `false`, the gateway returns HTTP 400 before calling the backend.

**Cross-backend matrix (documented, not normalized):**
| Backend | Tool calls in response | Streaming with tools | Notes |
|---|---|---|---|
| Ollama | Structured `tool_calls` for supported models (Llama 3.1+, Qwen2.5, Mistral Nemo, …) | Buggy on some models; verify per model | Only certain models. |
| MLX (`mlx_lm.server`) | Tool-call text inside `content` (model-templated) | Same | **Best-effort.** No structured `tool_calls` in v1. |
| Docker Model Runner | Structured `tool_calls` if model's GGUF chat template supports it | Yes | Depends on llama.cpp grammar + chat template. |

---

## 5. Backend Adapters

### 5.1 ABC

```python
class BackendAdapter(ABC):
    name: str
    base_url: str

    @abstractmethod
    async def chat_completions(
        self, body: dict, stream: bool
    ) -> Union[dict, AsyncIterator[bytes]]: ...

    @abstractmethod
    async def embeddings(self, body: dict) -> dict: ...

    @abstractmethod
    async def health(self) -> dict: ...

    async def close(self) -> None: ...
```

`NotSupportedError` is a custom exception caught in the routers and translated to HTTP 501.

### 5.2 Adapter defaults
| Adapter | Default base_url | OpenAI-compat path |
|---|---|---|
| Ollama | `http://host.docker.internal:11434` | `/v1/chat/completions`, `/v1/embeddings`, `/v1/models` |
| MLX | `http://host.docker.internal:8080` | `/v1/chat/completions` only — **no** `/v1/embeddings` |
| Docker Model Runner | `http://model-runner.docker.internal/engines/v1` | `/chat/completions`, `/embeddings`, `/models` |

Notes:
- Docker Model Runner: from a container the documented hostname is `model-runner.docker.internal` with **no port**. From host, `localhost:12434/engines/v1` works only if "Enable host-side TCP support" is on in Docker Desktop.
- MLX: each `mlx_lm.server` process serves **one model**. Multiple MLX models = multiple processes on different host ports, each listed separately in the registry.

### 5.3 Streaming implementation
- `httpx.AsyncClient.stream()` with `httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0)`.
- Iterate with `aiter_raw()` (NOT `aiter_lines()` — line buffering breaks UTF-8 mid-token).
- Wrap in FastAPI `StreamingResponse(..., media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})`.
- Pass through upstream `data: [DONE]\n\n` verbatim — do not synthesize.
- Cancellation: `try/finally` around the iterator closes the upstream stream when the client disconnects (otherwise we leak upstream connections during user-side cancels).

---

## 6. Configuration

### 6.1 `config/models.yaml`
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
  - id: mlx-mistral
    backend: mlx
    upstream_model: mlx-community/Mistral-7B-Instruct-v0.3
    base_url: http://host.docker.internal:8080
    capabilities: [chat]   # tools = best-effort, opt-in by setting [chat, tools]
  - id: model-runner-llama32
    backend: docker_model_runner
    upstream_model: ai/llama3.2
    capabilities: [chat, embeddings, tools]
```

`base_url` per-model overrides the adapter default — needed for MLX since each model is a separate process on its own port.

`capabilities` enum: `chat | embeddings | tools`.

Hot-reload: `watchfiles` watches the file; on change, the registry rebuilds atomically. No process restart needed.

### 6.2 `config/.env.example`
```env
GATEWAY_HOST=0.0.0.0
GATEWAY_PORT=8000
KEYS_DB_PATH=/var/lib/local-ai-server/keys.db
MODELS_YAML_PATH=/etc/local-ai-server/models.yaml
LOG_LEVEL=INFO
CORS_ORIGINS=                  # comma-separated; empty = deny all browser origins
LAN_IP=192.168.1.10            # used by Caddy bind
```

Loaded with `pydantic-settings`.

---

## 7. Authentication

### 7.1 Key format
- Plaintext: `sk-local-` + 32 random urlsafe bytes.
- Stored: `sha256_prefix(12 chars of plaintext)` + `argon2id_hash(plaintext)` + `created_at`, `last_used_at`, `name`.
- Plaintext is **never** persisted; only shown once at generation.

### 7.2 SQLite schema
```sql
CREATE TABLE api_keys (
  prefix      TEXT PRIMARY KEY,        -- 'sk-local-XXXX' first 12 chars
  hash        TEXT NOT NULL,           -- argon2id encoded form
  name        TEXT,
  created_at  INTEGER NOT NULL,
  last_used_at INTEGER,
  revoked_at  INTEGER
);
```

### 7.3 Verification flow (`app/auth.py`)
```python
PREFIX_LEN = 12

async def verify(token: str) -> bool:
    prefix = token[:PREFIX_LEN]
    row = db.get(prefix)
    if row is None or row.revoked_at is not None:
        return False
    try:
        argon2.PasswordHasher().verify(row.hash, token)
    except VerifyMismatchError:
        return False
    db.touch(prefix)  # update last_used_at
    return True
```

Middleware applies to all `/v1/*` routes; `/healthz` and `/readyz` are public.

### 7.4 Key generation script
```bash
python scripts/generate_api_key.py --name "claude-code"
# prints once: sk-local-AbCd...
```

---

## 8. Logging & Observability

Structured JSON via `structlog`, fields per request:
```json
{
  "ts": "2026-06-07T10:11:12Z",
  "level": "INFO",
  "event": "chat_completion",
  "key_prefix": "sk-local-AbCd",
  "model": "ollama-llama3",
  "backend": "ollama",
  "stream": true,
  "status": 200,
  "latency_ms": 1843,
  "prompt_tokens": 137,
  "completion_tokens": 412
}
```
- `Authorization` header redacted everywhere.
- Token counts pulled from upstream `usage` block (or a final SSE chunk for streaming).
- Stdout only; let Docker handle log files.

---

## 9. Containerization

### 9.1 `docker/Dockerfile.gateway`
```dockerfile
FROM python:3.12-slim AS base
WORKDIR /app
COPY docker/install_uv.sh /tmp/
RUN bash /tmp/install_uv.sh && useradd -r -u 10001 app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app/ ./app/
USER app
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
```

Reuses the existing `docker/install_uv.sh` for venv setup.

### 9.2 `compose.yaml`
```yaml
services:
  gateway:
    build:
      context: .
      dockerfile: docker/Dockerfile.gateway
    env_file: config/.env
    volumes:
      - ./config/models.yaml:/etc/local-ai-server/models.yaml:ro
      - keys-data:/var/lib/local-ai-server
    extra_hosts:
      - "host.docker.internal:host-gateway"
    networks: [internal]
    # NO published ports — only Caddy reaches it

  caddy:
    image: caddy:2
    depends_on: [gateway]
    ports:
      - "${LAN_IP}:443:443"
    volumes:
      - ./caddy/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    networks: [internal]

volumes:
  keys-data:
  caddy-data:
  caddy-config:

networks:
  internal:
```

`${LAN_IP}` from `.env` ensures Caddy binds to the LAN interface only — not `0.0.0.0`.

### 9.3 `caddy/Caddyfile`
```
{
    auto_https off
    admin off
}

https://ai.local {
    tls internal
    encode zstd gzip
    reverse_proxy gateway:8000 {
        flush_interval -1
        transport http {
            read_buffer 4KB
        }
    }
    log {
        output stdout
        format json
    }
}
```

`flush_interval -1` ensures SSE streams aren't buffered. `tls internal` uses Caddy's local CA — clients run `caddy trust` once to install the root cert. The hostname `ai.local` is set in client `/etc/hosts` (or via mDNS / router DNS).

---

## 10. Host-side backend lifecycle (`Makefile`)

Compose can't manage host-native processes; the Makefile fills the gap.

```makefile
.PHONY: start-ollama start-mlx start-model-runner stop-all status

start-ollama:
	@pgrep -x ollama >/dev/null || (ollama serve &) && echo "ollama: ok"

start-mlx:
	@test -n "$(MODEL)" || (echo "usage: make start-mlx MODEL=<huggingface_id> PORT=8080" && exit 1)
	@PORT=$${PORT:-8080}; \
	  uv run --with mlx-lm mlx_lm.server --model $(MODEL) --port $$PORT &

start-model-runner:
	@docker desktop status >/dev/null 2>&1 || (echo "Docker Desktop not running" && exit 1)
	@docker model status >/dev/null 2>&1 || (echo "Enable Model Runner in Docker Desktop settings" && exit 1)
	@echo "model runner: ok"

stop-all:
	@pkill -x ollama || true
	@pkill -f mlx_lm.server || true
```

`mlx_lm.server` is single-model per process; running multiple MLX models means launching multiple `start-mlx MODEL=... PORT=...` invocations and listing them in the registry with their respective `base_url`.

---

## 11. Dependencies (`pyproject.toml`)

```toml
[project]
name = "local-ai-server"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.32",
  "httpx>=0.27",
  "pydantic>=2.9",
  "pydantic-settings>=2.5",
  "argon2-cffi>=23.1",
  "pyyaml>=6.0",
  "watchfiles>=0.24",
  "structlog>=24.4",
]

[dependency-groups]
dev = [
  "pytest>=8",
  "pytest-asyncio>=0.24",
  "pytest-httpx>=0.30",
  "ruff>=0.6",
  "mypy>=1.11",
]
```

Dev-container `docker/requirements.txt` is also updated to include the runtime deps so the dev workflow has the same Python stack.

---

## 12. Security checklist

- API keys: Argon2id hashing; constant-time compare on hashes (the library does this); plaintext shown once.
- TLS: Caddy `tls internal`, root cert trusted on each LAN client.
- Bind: Caddy on LAN IP only; gateway has no published ports.
- Logs: redact `Authorization`; log only `key_prefix`.
- CORS: default deny; explicit allowlist via env.
- macOS firewall: README documents inbound rule for port 443 on LAN-only.
- Secrets: `.env`, `keys.db` gitignored.
- Path traversal / injection: not user-controllable inputs other than upstream proxy bodies, which we validate against Pydantic schemas before forwarding.

---

## 13. Implementation phases

| Phase | Scope | Deliverable |
|---|---|---|
| 1 | Skeleton | `pyproject.toml`, `app/main.py` healthz only, ABC + 3 stub adapters, registry loader |
| 2 | Endpoints | `/v1/models`, `/v1/chat/completions` (non-stream → streaming), `/v1/embeddings`, capability gating |
| 3 | Auth + obs | API-key middleware, key-gen scripts, structured logs, `/readyz` per-backend |
| 4 | Containers | `Dockerfile.gateway`, `compose.yaml`, `Caddyfile`, LAN binding |
| 5 | Host backends | `Makefile` for ollama / mlx / model-runner lifecycle |
| 6 | Tests + docs | pytest with httpx mocks, README rewrite, dev-container deps update |

---

## 14. Verification

End-to-end smoke test once implemented:

1. **Host setup:** `make start-ollama && make start-mlx MODEL=mlx-community/Mistral-7B-Instruct-v0.3 PORT=8080 && make start-model-runner`
2. **Containers:** `docker compose up --build`
3. **Mint key:** `python scripts/generate_api_key.py --name claude-code` → `sk-local-…` (saved by user)
4. **Trust cert:** `docker compose exec caddy caddy trust` on the gateway host; on each client copy `/data/caddy/pki/authorities/local/root.crt` and trust it.
5. **DNS:** add `<LAN_IP> ai.local` to client `/etc/hosts`.
6. **OpenAI SDK against `https://ai.local/v1` with the key:**
   - `client.models.list()` returns the registry.
   - `client.chat.completions.create(model="ollama-llama3", messages=[…])` → text out (non-streaming).
   - Same with `stream=True` → incremental chunks; final chunk contains `[DONE]`.
   - `client.chat.completions.create(model="mlx-mistral", …)` → text via MLX.
   - `client.embeddings.create(model="ollama-nomic-embed", input="hello")` → vector.
   - `client.embeddings.create(model="mlx-mistral", input="hello")` → 501 with descriptive body.
   - Missing/bad API key → 401.
   - Tool request to `mlx-mistral` (no `tools` capability) → 400 before reaching backend.
7. **Tests:** `uv run pytest` green; `uv run ruff check .` clean.
8. **Health:** `curl https://ai.local/healthz` → 200; `/readyz` reflects per-backend up/down.
9. **Hot-reload:** add a model to `models.yaml`, observe `/v1/models` reflects it within ~1s without restart.

---

## 15. Open follow-ups (post-v1)

- Per-key rate limits + model allowlists.
- Usage metering and Prometheus metrics endpoint.
- MLX tool-call normalization (parse text → `tool_calls`).
- Cross-backend embedding fallback / model aliases.
- Simple admin TUI for keys / models.
- Tailscale option (drop Caddy public binding, bind to Tailscale interface).
