## Project Summary: local-ai-server

**Description**: v0.1.0 is the first vertical slice of a home AI server on Mac Studio that exposes an OpenAI-compatible HTTP API and routes to a single local backend (Ollama). The full spec targets a TLS-fronted, multi-backend gateway (Ollama + MLX + Docker Model Runner) with API-key auth and Caddy; v0.1.0 is the minimal end-to-end path that proves the routing seam and OpenAI compatibility, with later versions layering on auth, containers, and the remaining backends.

### Key Requirements

**In scope for v0.1.0**
- FastAPI gateway skeleton with `/healthz` and the registry/loader plumbing (spec phase 1).
- OpenAI-compatible endpoints wired to **Ollama only**: `GET /v1/models`, `POST /v1/chat/completions` (non-streaming and SSE streaming), `POST /v1/embeddings`, with capability gating per `models.yaml` (spec phase 2).
- `BackendAdapter` ABC plus three concrete classes: `OllamaAdapter` is fully wired; `MLXAdapter` and `DockerModelRunnerAdapter` exist but every method returns HTTP 501 `not_implemented` to validate the ABC seam without committing implementation.
- Live integration tests against a real Ollama process on the host (no mocks on the Ollama path); pytest-httpx mocks acceptable for the trivial 501 stubs (spec phase 6).
- Notebooks demonstrating the user-facing endpoints (chat + embeddings + models list) in the endpoints phase.
- README rewrite scoped to v0.1.0 capabilities and the dev-container deps update.

**Out of scope for v0.1.0 (deferred to v0.2.0+)**
- API-key auth, Argon2id key store, key-mint scripts, `/readyz` (spec phase 3).
- Caddy reverse proxy, TLS, `Dockerfile.gateway`, `compose.yaml`, LAN binding (spec phase 4).
- `Makefile` for host-side backend lifecycle (spec phase 5).
- Real MLX and Docker Model Runner backend implementations (stubs only in v0.1.0).
- Hot-reload via `watchfiles`, structured logging fields, and tool-call gating beyond what falls out of capabilities.

### Proposed Technology Stack
- Python 3.12, FastAPI, uvicorn
- httpx (async client for adapters), pydantic v2, pydantic-settings
- structlog, watchfiles, pyyaml
- pytest (+ pytest-asyncio, pytest-httpx), ruff
- uv for env + dependency management

### Estimated Phases: 3
Mapped 1:1 to spec §13: (1) Skeleton, (2) Endpoints (Ollama wired, MLX + Model Runner stubbed at 501), (3) Tests + docs.

### Proposed Branch: dev/local-ai-server
