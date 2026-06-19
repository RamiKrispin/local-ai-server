# API Reference

**Project**: local-ai-server
**Version**: v0.1.0
**Default base URL**: `http://127.0.0.1:8000`
**Auth**: none in v0.1.0 (API-key auth lands in v0.2.0)

This is the public HTTP surface of the gateway. The `/v1/*` endpoints
mirror the OpenAI v1 wire format verbatim, so any OpenAI SDK client
(Python, Node, LangChain, etc.) works against the gateway by changing
only `base_url`. `/healthz` is a non-versioned liveness probe.

---

## Compatibility surface

| OpenAI surface | Status in v0.1.0 |
|---|---|
| `GET /v1/models` | implemented (lists registry entries) |
| `POST /v1/chat/completions` (stream + non-stream) | implemented for the Ollama backend |
| `POST /v1/embeddings` | implemented for the Ollama backend |
| `GET /v1/completions` (legacy) | not implemented |
| `GET /healthz` | implemented (non-versioned, no auth) |
| `GET /readyz` | not in v0.1.0 — see [`spec.md`](spec.md) §4.4 (lands in v0.2.0) |

All request/response Pydantic mirrors live in `app/schemas.py` and use
`extra="allow"` so unknown OpenAI vendor fields pass through unchanged.

---

## `GET /healthz`

Liveness probe. Always returns 200 if the gateway process is up.
Implemented in `app/routers/health.py:6-9`.

**Request**

```http
GET /healthz HTTP/1.1
Host: 127.0.0.1:8000
```

**Response (200 OK)**

```json
{"status": "ok"}
```

- No auth.
- No upstream pings (this is a liveness probe, not a readiness probe).
- No `/v1` prefix.

---

## `GET /v1/models`

List the models declared in `config/models.yaml` as an OpenAI list
response. Implemented in `app/routers/models.py:10-24`.

**Request**

```http
GET /v1/models HTTP/1.1
Host: 127.0.0.1:8000
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
- No auth in v0.1.0.

---

## `POST /v1/chat/completions`

OpenAI-compatible chat completions, supporting both non-streaming JSON
responses and SSE streaming. Implemented in `app/routers/chat.py:18-89`.

**Request body**

The request body is validated against `ChatCompletionRequest`
(`app/schemas.py:64-83`). The schema sets `extra="allow"`, so any
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
(see `app/adapters/ollama.py:50-67`). The gateway adds three headers
to defeat upstream/middlebox buffering (see
`app/routers/chat.py:9-13`):

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

---

## `POST /v1/embeddings`

OpenAI-compatible embeddings. Implemented in
`app/routers/embeddings.py:11-52`.

**Request body**

Validated against `EmbeddingsRequest` (`app/schemas.py:157-162`).

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

---

## Error envelope

All non-2xx responses use the OpenAI-compatible error envelope, built
by `make_error()` in `app/errors.py:26-40`. The shape is:

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

Four error types are produced in v0.1.0:

### `not_supported` (HTTP 501)

Emitted by `not_supported_handler` (`app/errors.py:83-93`) when an
adapter raises `NotSupportedError`, or when a router's capability gate
fires.

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
  Docker Model Runner in v0.1.0).

### `invalid_request_error` (HTTP 400)

Emitted by `http_exception_handler` (`app/errors.py:63-80`) for the
tools gate, and for `model_not_found` (which uses HTTP 404; see below).

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

### `invalid_request_error` (HTTP 404 for unknown model)

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

### `invalid_request_error` (HTTP 422 for malformed body)

Emitted by `validation_exception_handler` (`app/errors.py:96-116`)
when Pydantic rejects the request body. `param` carries the dotted
path of the first invalid field; `code` is `null`.

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

### `internal_error` (HTTP 500)

Emitted by `unhandled_exception_handler` (`app/errors.py:119-135`)
when anything else raises (e.g., Ollama unreachable, upstream 5xx).
The traceback is logged at ERROR level; it is never serialized into
the response body.

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

For chat completions, the gates fire in this order. The capability
gate always fires before the tools gate.

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

| Model id | Backend | Status in v0.1.0 |
|---|---|---|
| `ollama-llama3` | ollama | wired (chat + tools) |
| `ollama-nomic-embed` | ollama | wired (embeddings) |
| `mlx-mistral` | mlx | stub — every method returns 501 |
| `model-runner-llama32` | docker_model_runner | stub — every method returns 501 |

The MLX and Docker Model Runner adapters are present to lock the
`BackendAdapter` ABC seam. Real implementations land in v0.4.0+
(see [`spec.md`](spec.md) §13).

`MLXAdapter.embeddings()` raises with
`code="backend_capability_missing"` (a permanent capability gap per
[`spec.md`](spec.md) §5.2); all other stub methods raise with
`code="not_implemented"`.

---

## Runnable examples

A complete end-to-end demo using the OpenAI Python SDK lives at
[`examples/v0_1_0_demo.ipynb`](../examples/v0_1_0_demo.ipynb). It
covers `models.list`, non-streaming chat, streaming chat with delta
concatenation, embeddings, and a 501 example from a stub backend.

For step-by-step recipes, see [`usage-guide.md`](usage-guide.md).
