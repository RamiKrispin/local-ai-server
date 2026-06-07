# Local AI Server

> **Status: Work in Progress.** This project is under active development — the spec is finalized but the implementation is still being built out. Interfaces, configuration, and layout may change.

A home AI server for a Mac Studio that exposes an **OpenAI-compatible HTTP API** and routes requests to multiple **local LLM backends**: Ollama, MLX (`mlx_lm.server`), and Docker Model Runner.

Any OpenAI SDK client (Python, Node, LangChain, etc.) can talk to it with **zero code changes** — only `base_url` and `api_key` differ.

## Architecture

![Design diagram](assets/design%20diagram.png)

The gateway and Caddy reverse proxy run in containers; the three model backends run **natively on the host** (Apple Metal has no GPU passthrough into Docker on Mac, and MLX has no Linux wheels). Containers reach the host via `host.docker.internal` and `model-runner.docker.internal`.

- **Caddy** — terminates TLS (`tls internal`) and binds to the LAN interface only.
- **FastAPI gateway** — auth, model registry lookup, schema validation, structured logging, and OpenAI-compatible request/response shapes.
- **Adapter layer** — one async httpx adapter per backend (Ollama, MLX, Docker Model Runner) behind a common ABC.
- **Model registry** — YAML mapping logical model id → backend + upstream model + capabilities (`chat`, `embeddings`, `tools`), hot-reloaded via `watchfiles`.
- **Auth** — bearer API keys (`sk-local-…`) stored as Argon2id hashes in SQLite; plaintext shown once at generation.

## API

All endpoints under `/v1/`, wire-compatible with OpenAI v1:

- `GET /v1/models` — union of enabled models from `models.yaml`
- `POST /v1/chat/completions` — streaming and non-streaming
- `POST /v1/embeddings` — routed only to backends with the `embeddings` capability (501 otherwise)
- `GET /healthz`, `GET /readyz` — unauthenticated; `/readyz` reports per-backend status

## Documentation

The full design is in [`docs/spec.md`](docs/spec.md), including the request lifecycle, repository layout, adapter contracts, configuration schema, auth flow, container/Caddy setup, host-side `Makefile` for backend lifecycle, and the end-to-end verification plan.

## License

[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/).
