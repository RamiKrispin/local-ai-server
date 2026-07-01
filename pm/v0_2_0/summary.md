## Project Summary: local-ai-server v0.2.0 — Auth + Observability

**Description**: v0.2.0 layers API-key authentication and structured observability onto the working v0.1.0 OpenAI-compatible gateway, hardening the LAN-internal seam that v0.1.0 deliberately left open.

### Baseline

v0.2.0 starts from `dev/local-ai-server` post-v0.1.0 merge (PR #1 against `main`). The dev branch is the natural integration line for the next version — phase branches will follow the same `phase/local-ai-server/<N>-<name>` convention used in v0.1.0 and merge back into `dev/local-ai-server`. No new dev branch is needed; v0.2.0 is the second version of the same project. The wired v0.1.0 surface (Ollama adapter, three `/v1` routers, lifespan-built `app.state.adapters` dict, OpenAI error envelope, `OllamaAdapter.health()` already implemented per ABC) is the seam Phase 2 will extend.

### Key Requirements

**IN scope (v0.2.0):**
- **API-key auth** (spec §7): `argon2-cffi` password hashing, SQLite key store with the spec §7.2 schema (`prefix` PK + `argon2id_hash` + `created_at`/`last_used_at`/`revoked_at`), `app/auth.py` bearer middleware on all `/v1/*` routes (mounts via `app.middleware("http")` between exception handlers and routers), `scripts/generate_api_key.py` and `scripts/revoke_api_key.py` CLI tools. `/healthz` and `/readyz` remain unauthenticated.
- **`/readyz` per-backend health composition** (spec §4.4): new `health.py` route that fans out to every adapter's `health()` method (already on the ABC and already implemented for Ollama in v0.1.0); returns 200 if any backend reachable, payload lists per-backend status. The seam is already wired — v0.2.0 just composes it.
- **Structured JSON logging via `structlog`** (spec §8): replace stdlib `logging` with `structlog` everywhere; per-request fields `key_prefix`, `model`, `backend`, `stream`, `status`, `latency_ms`, `prompt_tokens`, `completion_tokens`; `Authorization` header redaction global; stdout-only (Docker handles files).
- **Hot-reload of `config/models.yaml` via `watchfiles`** (spec §6.1): registry rebuilds atomically on file change; no process restart needed; tested by mutating the file and asserting `/v1/models` reflects the change within ~1s.
- **mypy `--strict` cleanup**: the 27 findings carried forward from v0.1.0 Phase 3 (flagged in `pm/v0_1_0/phase-3-architecture.md` §11 row 2).

**OUT of scope (deferred):**
- Caddy / TLS / `compose.yaml` / `Dockerfile.gateway` / LAN binding → **v0.3.0**.
- Real MLX (`mlx_lm.server`) and Docker Model Runner adapter implementations, host-side `Makefile` for backend lifecycle → **v0.4.0+**.
- Per-key rate limits, quotas, usage metering, Prometheus → post-v1 (spec §15).

### Proposed Technology Stack Additions

Existing v0.1.0 stack stays put (FastAPI, uvicorn, httpx, pydantic, pydantic-settings, pyyaml, pytest + pytest-asyncio + pytest-httpx, ruff, mypy). New runtime dependencies:

- **`argon2-cffi>=23.1`** — Argon2id password hashing per spec §7.1.
- **`structlog>=24.4`** — structured JSON logging per spec §8.
- **`watchfiles>=0.24`** — hot-reload of `config/models.yaml` per spec §6.1.

All three are already enumerated in spec §11 — v0.2.0 just adopts them.

New env vars (spec §6.2): `KEYS_DB_PATH` (SQLite path, defaults to a repo-local `./data/keys.db` for v0.2.0 since containerization is v0.3.0). `CORS_ORIGINS` and `LAN_IP` remain deferred to v0.3.0.

### Proposed Phase Count: 3

**Justification**: Auth and observability are independent slices architecturally — auth wires a new middleware + DB + scripts, observability rewires the logging stack and composes existing `health()` calls. They share no code but share a regression surface (auth needs structured logs to redact `Authorization`; `/readyz` is unauthenticated and tests must verify that). Bundling tests + mypy cleanup + docs into a single closing phase keeps the integration cycle clean and matches the v0.1.0 cadence (3 phases, last phase tests-and-docs only, no `app/` regressions).

| Phase | Name | Scope summary |
|------:|------|---------------|
| 1 | **Auth foundation** | `app/auth.py` middleware (bearer + Argon2id verify); `scripts/generate_api_key.py` + `scripts/revoke_api_key.py`; SQLite key store with spec §7.2 schema; middleware mount in `create_app()` between `install_exception_handlers(app)` and the `/v1/*` routers; `KEYS_DB_PATH` setting; demo notebook for the user-facing key-mint + authenticated request flow. |
| 2 | **Observability** | `structlog` migration (replace stdlib `logging` everywhere; redact `Authorization`); `/readyz` per-backend composition that iterates `app.state.adapters[*].health()`; `watchfiles` hot-reload of `config/models.yaml` with atomic registry swap; demo notebook for `/readyz` + hot-reload (auth notebook from P1 covers the auth surface). |
| 3 | **Tests + mypy + docs** | Full live integration tests for auth (401/403/200 paths) and `/readyz` (per-backend health aggregation; pytest-httpx mocks for unreachable-backend cases); regression tests for hot-reload (mutate yaml, assert `/v1/models` shifts) and structlog redaction (assert no plaintext `Authorization` in log records); the 27 carried-forward `mypy --strict` cleanups; README v0.2.0 update; `docker/requirements.txt` parity for the three new deps. |

The v0.1.0 test strategy carries forward verbatim: live integration tests against a real Ollama where applicable; `pytest.mark.live` + the conftest auto-skip hook; pytest-httpx mocks reserved for offline/unreachable paths; notebooks opt-in for phases with user-facing surface (auth in P1, `/readyz` in P2; structlog and hot-reload are infra-only and don't need notebooks).

### Proposed Branch Convention

Same as v0.1.0: `phase/local-ai-server/<N>-<name>` branched off `dev/local-ai-server`, merged back via PR after the test checkpoint passes. Specifically:
- `phase/local-ai-server/1-auth`
- `phase/local-ai-server/2-observability`
- `phase/local-ai-server/3-tests-mypy-docs`

The version-bump PR against `main` (containing all three merged phases plus updated `pm/project-state.md`) follows the v0.1.0 PR pattern.

### Estimated Phases: 3
### Proposed Branch (continuing): dev/local-ai-server
