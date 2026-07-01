# API Reference

**Project**: local-ai-server
**Version**: v0.2.0
**Default base URL**: `http://127.0.0.1:8000`
**Auth**: Bearer `sk-local-...` on all `/v1/*` routes (see [Authentication](#authentication))

This is the public HTTP surface of the gateway. The `/v1/*` endpoints
mirror the OpenAI v1 wire format verbatim, so any OpenAI SDK client
(Python, Node, LangChain, etc.) works against the gateway by changing
only `base_url` and `api_key`. `/healthz` and `/readyz` are
non-versioned probes and remain public.

---

## Compatibility surface

| OpenAI surface | Status in v0.2.0 | Auth |
|---|---|---|
| `GET /v1/models` | implemented (reflects `config/models.yaml` changes within ~1s without restart) | required |
| `POST /v1/chat/completions` (stream + non-stream) | implemented for the Ollama backend | required |
| `POST /v1/embeddings` | implemented for the Ollama backend | required |
| `GET /v1/completions` (legacy) | not implemented | n/a |
| `GET /healthz` | implemented (liveness only) | public |
| `GET /readyz` | implemented (per-backend health fan-out; new in v0.2.0) | public |

All request/response Pydantic mirrors live in `app/schemas.py` and use
`extra="allow"` so unknown OpenAI vendor fields pass through unchanged.

---

## Authentication

Bearer-token authentication gates every `/v1/*` route. The middleware
lives in `app/auth.py:149-250` (`BearerAuthMiddleware`) and verifies
the token against an Argon2id hash stored in the SQLite key store at
`KEYS_DB_PATH` (default `./data/keys.db`).

### Header format

```
Authorization: Bearer sk-local-<urlsafe-token>
```

- Scheme matching is case-insensitive (`Bearer`, `bearer`, `BEARER`
  all work; see `_BEARER_RE` in `app/auth.py:136`).
- Tokens are minted via `scripts/generate_api_key.py` and have the
  shape `sk-local-` + 32 URL-safe random bytes (`secrets.token_urlsafe`).
- The first 12 characters of the plaintext form the `prefix` (the
  SQLite primary key); the full plaintext is never persisted.

### Public path allowlist

The middleware skips auth on these exact paths (`PUBLIC_PATHS` in
`app/auth.py:138-146`):

- `/healthz`
- `/readyz`
- `/docs`
- `/openapi.json`
- `/redoc`

Matching is exact-string only, not prefix-based. Every other path,
including all of `/v1/*`, requires a valid bearer token.

### 401 envelope (verbatim)

All five auth-failure paths (missing header, malformed header, unknown
prefix, revoked key, hash mismatch) return HTTP 401 with the OpenAI
error envelope. The body shape is:

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "Invalid API key",
    "param": "Authorization",
    "code": "invalid_api_key"
  }
}
```

The `message` field varies by failure mode for ops debuggability:

| Trigger | `message` |
|---|---|
| Missing `Authorization` header | `"Missing Authorization header"` |
| Malformed header (wrong scheme, empty token, etc.) | `"Malformed Authorization header"` |
| Unknown prefix (not in keys.db) | `"Invalid API key"` |
| Key revoked (`revoked_at IS NOT NULL`) | `"Invalid API key"` |
| Argon2 verify mismatch | `"Invalid API key"` |

SDK clients key off `code="invalid_api_key"`; the message strings are
not load-bearing for retry/backoff logic. The unknown-prefix and
revoked-key cases share the same message deliberately so a probing
client cannot distinguish whether a prefix exists.

The middleware does **not** emit a `WWW-Authenticate: Bearer` response
header on 401 — OpenAI's upstream 401 also omits it.

### last-used tracking

Every successful request updates `api_keys.last_used_at` to
`int(time.time())` via the `touch()` helper (`app/auth.py:105-115`).
Inspect via:

```bash
sqlite3 data/keys.db \
  "SELECT prefix, name, datetime(last_used_at, 'unixepoch') FROM api_keys"
```

### CLIs

- `uv run python scripts/generate_api_key.py --name <label>` —
  prints `sk-local-...` exactly once to stdout; also writes the
  Argon2id hash to keys.db. A second courtesy line goes to stderr.
- `uv run python scripts/revoke_api_key.py --prefix <prefix>` — sets
  `revoked_at = now`; exits 0 on success, 1 on unknown prefix.

---

## `GET /healthz`

**Auth**: public.

Liveness probe. Always returns 200 if the gateway process is up.
Implemented in `app/routers/health.py:18-21`.

**Request**

```http
GET /healthz HTTP/1.1
Host: 127.0.0.1:8000
```

**Response (200 OK)**

```json
{"status": "ok"}
```

- No auth, no upstream pings.
- No `/v1` prefix.

The request still flows through `RequestLoggingMiddleware`, which
emits one structured `event="request"` log entry per call (see
[Structured logging](#structured-logging) below). `key_prefix` is
`null` because auth was skipped.

---

## `GET /readyz`

**Auth**: public.

Readiness probe that fans out concurrently over every adapter in
`app.state.adapters`, calling each one's `health()` method via
`asyncio.gather(..., return_exceptions=True)`. Implemented in
`app/routers/health.py:24-89`.

**Aggregation rule** (binary, no `degraded` intermediate state):

- 200 if **at least one** backend's payload contains `status == "ok"`.
- 503 if every backend is `unreachable`, `error`, or otherwise not
  reporting `ok`.

**Request**

```http
GET /readyz HTTP/1.1
Host: 127.0.0.1:8000
```

**Response (200 OK)** — any backend reachable:

```json
{
  "status": "ok",
  "backends": {
    "docker_model_runner": {
      "status": "error",
      "error": "NotSupportedError: Docker Model Runner adapter is not implemented in v0.1.0"
    },
    "mlx": {
      "status": "error",
      "error": "NotSupportedError: MLX adapter is not implemented in v0.1.0"
    },
    "ollama": {
      "status": "ok",
      "models": ["llama3.1:8b", "nomic-embed-text"]
    }
  }
}
```

The MLX and Docker Model Runner adapter stubs raise
`NotSupportedError` from `health()`; the gateway catches that and
maps it to a uniform `{"status": "error", "error": "<TypeName>: <message>"}`
payload (see `_short_error` in `app/routers/health.py:13-15`).

**Response (503 Service Unavailable)** — all backends down:

```json
{
  "error": {
    "type": "service_unavailable",
    "message": "No backends reachable",
    "param": null,
    "code": "no_backends_reachable"
  },
  "backends": {
    "docker_model_runner": {
      "status": "error",
      "error": "NotSupportedError: Docker Model Runner adapter is not implemented in v0.1.0"
    },
    "mlx": {
      "status": "error",
      "error": "NotSupportedError: MLX adapter is not implemented in v0.1.0"
    },
    "ollama": {
      "status": "unreachable",
      "error": "All connection attempts failed"
    }
  }
}
```

The `error` block matches the OpenAI envelope verbatim (4 keys:
`type`, `message`, `param`, `code`). The top-level `backends` key is
a v0.2.0 spec extension for operational debugging; SDK clients
ignore unknown top-level fields.

---

## `GET /v1/models`

**Auth**: `Authorization: Bearer sk-local-...` required.

List the models declared in `config/models.yaml` as an OpenAI list
response. Implemented in `app/routers/models.py`.

**Hot-reload**: the gateway watches `MODELS_YAML_PATH` via
`watchfiles` (see `app/registry_watcher.py`). Edits saved to the file
are reflected in this endpoint's response within ~1 s without
restarting the process. See the architecture document's "Hot-reload
swap discipline" section for the atomic-swap details.

**Request**

```http
GET /v1/models HTTP/1.1
Host: 127.0.0.1:8000
Authorization: Bearer sk-local-...
```

**Response (200 OK)**

```json
{
  "object": "list",
  "data": [
    {
      "id": "ollama-llama3",
      "object": "model",
      "created": 1717000000,
      "owned_by": "local"
    },
    {
      "id": "ollama-nomic-embed",
      "object": "model",
      "created": 1717000000,
      "owned_by": "local"
    },
    {
      "id": "mlx-mistral",
      "object": "model",
      "created": 1717000000,
      "owned_by": "local"
    },
    {
      "id": "model-runner-llama32",
      "object": "model",
      "created": 1717000000,
      "owned_by": "local"
    }
  ]
}
```

- Lists all four registry entries, including stub-backed ones
  (`mlx-mistral`, `model-runner-llama32`). Capability gates fire only
  on the request endpoints, not on the listing.
- `created` is the current Unix timestamp at request time.

**Log fields** (per spec §8): emitted by `RequestLoggingMiddleware`
with `event="request"`, `path="/v1/models"`, `method="GET"`,
`key_prefix=<12-char>`, `status=200`, `latency_ms=<int>`. `model`,
`backend`, `stream`, `prompt_tokens`, `completion_tokens` are `null`
for this endpoint (no request body to inspect, no upstream call).

---

## `POST /v1/chat/completions`

**Auth**: `Authorization: Bearer sk-local-...` required.

OpenAI-compatible chat completions, supporting both non-streaming JSON
responses and SSE streaming. Implemented in `app/routers/chat.py`.

**Request body**

The request body is validated against `ChatCompletionRequest`
(`app/schemas.py`). The schema sets `extra="allow"`, so any
OpenAI vendor field not listed below is forwarded to the upstream
backend unchanged.

| Field | Type | Required | Notes |
|---|---|---|---|
| `model` | string | yes | Registry id (e.g., `"ollama-llama3"`), not the upstream Ollama tag. |
| `messages` | array of message objects | yes | At least one message; standard OpenAI roles `system`, `user`, `assistant`, `tool`, `developer`. |
| `stream` | bool | no (default `false`) | When `true`, response is SSE; when `false`, response is JSON. |
| `temperature` | float | no | Forwarded to upstream. |
| `top_p` | float | no | Forwarded. |
| `max_tokens` | int | no | Forwarded. |
| `max_completion_tokens` | int | no | Forwarded. |
| `n` | int | no | Forwarded. |
| `stop` | string \| array of strings | no | Forwarded. |
| `seed` | int | no | Forwarded. |
| `presence_penalty` | float | no | Forwarded. |
| `frequency_penalty` | float | no | Forwarded. |
| `logit_bias` | object | no | Forwarded. |
| `user` | string | no | Forwarded. |
| `response_format` | object | no | Forwarded. |
| `tools` | array of tool definitions | no | Triggers the tools gate; see below. |
| `tool_choice` | `"none"` \| `"auto"` \| `"required"` \| object | no | Forwarded. |
| `parallel_tool_calls` | bool | no | Forwarded. |
| `stream_options` | object | no | Forwarded. |

For the full OpenAI field semantics, see the
[OpenAI Chat Completions API reference](https://platform.openai.com/docs/api-reference/chat/create).

**Non-streaming response (200 OK)**

`application/json`. The body is whatever the upstream backend returned,
which for Ollama is the OpenAI v1 chat-completion shape:

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 1717000000,
  "model": "llama3.1:8b",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "Hi there!"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 12,
    "completion_tokens": 3,
    "total_tokens": 15
  }
}
```

Note that the `model` field on the response carries the **upstream**
model name (e.g., `llama3.1:8b`), not the registry id (e.g.,
`ollama-llama3`). This matches Ollama's own response shape and what
OpenAI SDK clients expect.

**Streaming response (200 OK)**

`text/event-stream`. Bytes from the upstream Ollama stream are
forwarded verbatim via `httpx.AsyncClient.stream(...).aiter_raw()`
(see `app/adapters/ollama.py`). The gateway adds three headers
to defeat upstream/middlebox buffering (see `app/routers/chat.py`):

```
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no
Connection: keep-alive
```

The body is a sequence of SSE events of the shape
`data: {json}\n\n`, terminated by `data: [DONE]\n\n`. The terminator
is **passed through verbatim** from Ollama; it is never synthesized
by the gateway.

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":" there"},"finish_reason":null}]}

data: [DONE]

```

If the consumer disconnects partway through, the adapter's
`async with self._client.stream(...)` block closes the upstream
response, releasing the connection.

**Log fields** (per spec §8): `RequestLoggingMiddleware` emits one
`event="request"` entry per call with `path="/v1/chat/completions"`,
`method="POST"`, `key_prefix=<12-char>`, `model=<registry id>`,
`backend=<resolved via registry>`, `stream=<bool>`, `status=<int>`,
`latency_ms=<int>`. Token counts (`prompt_tokens`,
`completion_tokens`) come from the upstream `usage` block when
present:

- Non-streaming: parsed from `response.json()["usage"]`.
- Streaming: parsed from the most recent non-`[DONE]` SSE chunk if
  upstream emits a `usage` field on it. When absent, both token
  fields are emitted as JSON `null` — the gateway does not synthesize
  or estimate counts.

---

## `POST /v1/embeddings`

**Auth**: `Authorization: Bearer sk-local-...` required.

OpenAI-compatible embeddings. Implemented in `app/routers/embeddings.py`.

**Request body**

Validated against `EmbeddingsRequest` (`app/schemas.py`).

| Field | Type | Required | Notes |
|---|---|---|---|
| `model` | string | yes | Registry id (e.g., `"ollama-nomic-embed"`). |
| `input` | string \| array of strings \| array of ints \| array of arrays of ints | yes | One or many inputs to embed. |
| `encoding_format` | `"float"` \| `"base64"` | no | Forwarded. |
| `dimensions` | int | no | Forwarded. |
| `user` | string | no | Forwarded. |

The model must declare `embeddings` in its capability list in
`config/models.yaml`; otherwise the gateway returns 501 before
contacting the backend.

**Response (200 OK)**

`application/json`. Forwarded from Ollama; matches the OpenAI v1
embeddings shape:

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "index": 0,
      "embedding": [0.0123, -0.0456, ...]
    }
  ],
  "model": "nomic-embed-text",
  "usage": {
    "prompt_tokens": 2,
    "total_tokens": 2
  }
}
```

For `nomic-embed-text` the embedding vector has length 768; other
embedding models produce different dimensions.

**Log fields**: same as chat completions, but `stream` is always
`false` for embeddings, and `completion_tokens` is typically `null`
because the OpenAI embeddings `usage` block carries only
`prompt_tokens` and `total_tokens`.

---

## Error envelope

All non-2xx responses use the OpenAI-compatible error envelope, built
by `make_error()` in `app/errors.py`. The shape is:

```json
{
  "error": {
    "type": "<error type>",
    "message": "<human-readable message>",
    "param": "<field name or null>",
    "code": "<machine-readable code or null>"
  }
}
```

The error types produced by v0.2.0:

### `invalid_request_error` (HTTP 401)

New in v0.2.0. Emitted by `BearerAuthMiddleware` on any of the five
auth-failure paths. See [Authentication](#authentication) above for
the exact envelope and per-trigger message table.

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "Invalid API key",
    "param": "Authorization",
    "code": "invalid_api_key"
  }
}
```

### `not_supported` (HTTP 501)

Emitted by `not_supported_handler` when an adapter raises
`NotSupportedError`, or when a router's capability gate fires.

```json
{
  "error": {
    "type": "not_supported",
    "message": "Backend 'mlx' / model 'mlx-mistral' does not support chat",
    "param": "model",
    "code": "backend_capability_missing"
  }
}
```

`code` is one of:
- `backend_capability_missing` — the registry says the model lacks
  the capability requested by the endpoint.
- `not_implemented` — the request reached a stub adapter (MLX or
  Docker Model Runner in v0.2.0).

### `invalid_request_error` (HTTP 400)

Emitted by `http_exception_handler` for the tools gate.

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "Model 'ollama-nomic-embed' does not support tool calls",
    "param": "tools",
    "code": "tools_not_supported"
  }
}
```

### `invalid_request_error` (HTTP 404)

For unknown models:

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "Unknown model 'does-not-exist'",
    "param": "model",
    "code": "model_not_found"
  }
}
```

### `invalid_request_error` (HTTP 422)

Emitted by `validation_exception_handler` when Pydantic rejects the
request body. `param` carries the dotted path of the first invalid
field; `code` is `null`. Auth runs before validation (see the
middleware install order in the architecture document), so even a
422 carries `key_prefix` in its log line.

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "Field required",
    "param": "body.messages",
    "code": null
  }
}
```

### `service_unavailable` (HTTP 503)

New in v0.2.0. Emitted by `/readyz` when every backend is unreachable.
See [`GET /readyz`](#get-readyz) above for the full body shape
including the top-level `backends` extension.

### `internal_error` (HTTP 500)

Emitted by `unhandled_exception_handler` when anything else raises
(e.g., Ollama unreachable, upstream 5xx). The traceback is logged at
ERROR level (and re-rendered as JSON via the structlog stdlib bridge);
it is never serialized into the response body.

```json
{
  "error": {
    "type": "internal_error",
    "message": "Internal server error",
    "param": null,
    "code": "internal_error"
  }
}
```

---

## Capability and tools gating decision table

For chat completions, the gates fire in this order, after the auth
check has already passed. The capability gate always fires before
the tools gate.

| Model in registry | Has `chat`? | Request has `tools`? | Has `tools`? | Status | `error.code` |
|---|---|---|---|---|---|
| not in registry | n/a | n/a | n/a | 404 | `model_not_found` |
| `ollama-nomic-embed` | no | no | n/a | 501 | `backend_capability_missing` |
| `ollama-nomic-embed` | no | yes | n/a | **501** (capability fires first) | `backend_capability_missing` |
| `mlx-mistral` | yes | yes | no | 400 | `tools_not_supported` |
| `mlx-mistral` | yes | no | n/a | 501 (stub adapter) | `not_implemented` |
| `model-runner-llama32` | yes | yes | yes | 501 (stub adapter) | `not_implemented` |
| `ollama-llama3` | yes | yes | yes | 200 | n/a |
| `ollama-llama3` | yes | no | n/a | 200 | n/a |

For embeddings:

| Model in registry | Has `embeddings`? | Status | `error.code` |
|---|---|---|---|
| not in registry | n/a | 404 | `model_not_found` |
| `ollama-llama3` | no | 501 | `backend_capability_missing` |
| `mlx-mistral` | no | 501 (capability gate fires) | `backend_capability_missing` |
| `model-runner-llama32` | yes | 501 (stub adapter) | `not_implemented` |
| `ollama-nomic-embed` | yes | 200 | n/a |

---

## Backend matrix

| Model id | Backend | Status in v0.2.0 |
|---|---|---|
| `ollama-llama3` | ollama | wired (chat + tools) |
| `ollama-nomic-embed` | ollama | wired (embeddings) |
| `mlx-mistral` | mlx | stub — every method returns 501 |
| `model-runner-llama32` | docker_model_runner | stub — every method returns 501 |

The MLX and Docker Model Runner adapters are present to lock the
`BackendAdapter` ABC seam. Real implementations land in v0.4.0+
(see [`spec.md`](spec.md) §13). The stubs' `health()` methods raise
`NotSupportedError`, which `/readyz` translates to a uniform
`{"status": "error", ...}` payload (see [`GET /readyz`](#get-readyz)).

`MLXAdapter.embeddings()` raises with
`code="backend_capability_missing"` (a permanent capability gap per
[`spec.md`](spec.md) §5.2); all other stub methods raise with
`code="not_implemented"`.

---

## Structured logging

Every HTTP request emits one `event="request"` log line on stdout,
JSON-rendered by `structlog` (`app/logging.py`,
`app/middleware_logging.py`). Field schema (per spec §8):

```json
{
  "ts": "2026-06-20T10:11:12Z",
  "level": "info",
  "event": "request",
  "path": "/v1/chat/completions",
  "method": "POST",
  "key_prefix": "sk-local-AbC",
  "model": "ollama-llama3",
  "backend": "ollama",
  "stream": true,
  "status": 200,
  "latency_ms": 1843,
  "prompt_tokens": 137,
  "completion_tokens": 412
}
```

Field source and nullability:

| Field | Source | Nullable when |
|---|---|---|
| `ts` | `TimeStamper(fmt="iso", utc=True)` | never |
| `level` | structlog `add_log_level` (lowercase string) | never |
| `event` | always `"request"` for request-log lines | never |
| `path` | `scope["path"]` | never |
| `method` | `scope["method"]` | never |
| `key_prefix` | `scope["state"]["key_prefix"]` set by auth middleware | public paths (auth skipped); non-`/v1/*` paths |
| `model` | parsed from cached request body JSON (chat/embeddings only) | non-inspectable paths; body parse failed |
| `backend` | `app.state.registry.get(model).backend` | `model` is `null` or not in registry |
| `stream` | `body.stream` for chat; `false` for embeddings; `null` elsewhere | non-chat/embeddings paths |
| `status` | wrapped `send`'s `http.response.start` | never (defaults to 500) |
| `latency_ms` | `time.perf_counter()` delta in ms (int) | never |
| `prompt_tokens` | upstream `usage.prompt_tokens` (int) | upstream did not emit `usage`; streaming without trailing usage chunk |
| `completion_tokens` | upstream `usage.completion_tokens` (int) | same; also `null` for embeddings |

The `Authorization` header is **never** logged verbatim — the
`redact_authorization` processor (`app/logging.py:32-75`) scrubs any
value starting with `Bearer ` to `<redacted: sk-local-XXX>` (preserving
the 12-char prefix where extractable) on every event_dict before
rendering. The redaction applies to both structlog-native call sites
and stdlib-`logging` call sites (the latter via the
`structlog.stdlib.ProcessorFormatter` bridge).

---

## Runnable examples

Two notebooks demonstrate the v0.2.0 surfaces end-to-end:

- [`posts/v0_2_0_auth_demo.ipynb`](../posts/v0_2_0_auth_demo.ipynb) —
  mint a key, make an authenticated SDK call, demonstrate the 401
  path.
- [`posts/v0_2_0_readyz_hotreload_demo.ipynb`](../posts/v0_2_0_readyz_hotreload_demo.ipynb) —
  probe `/readyz`, mutate `config/models.yaml` while the gateway is
  running, observe `/v1/models` reflect the change.

For step-by-step recipes, see [`usage-guide.md`](usage-guide.md).
