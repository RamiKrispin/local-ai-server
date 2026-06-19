# Usage Guide

A how-to companion to the [API reference](api-reference.md). The
README's quick-start gets you booted; this guide walks through common
tasks, configuration, and troubleshooting.

---

## Mental model

Every request flows through three layers:

1. **Router** (`app/routers/`) — validates the request body against
   a Pydantic schema, looks up the model in the registry, runs the
   capability and tools gates, then dispatches.
2. **Registry** (`app/registry.py`) — parsed once at startup from
   `config/models.yaml`. Maps the request's logical model id (e.g.,
   `ollama-llama3`) to a backend (`ollama`) and an upstream model
   name (`llama3.1:8b`).
3. **Adapter** (`app/adapters/`) — an async `httpx` client that
   forwards the request to the upstream backend (Ollama for
   v0.1.0). Streaming responses are passed through verbatim using
   `aiter_raw()`.

When a request comes in, the registry-facing model id is rewritten
to the upstream model name before the adapter call, so Ollama
receives `llama3.1:8b`, not `ollama-llama3`.

For the full lifecycle and seam diagrams see
[`architecture.md`](architecture.md).

---

## One-time setup

### 1. Install Ollama and pull the test models

```bash
# install Ollama from https://ollama.com or via brew
brew install ollama

# start the Ollama server (leave running)
ollama serve

# in another shell, pull the two models the gateway uses
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

Verify Ollama is reachable:

```bash
curl http://localhost:11434/api/tags
```

You should see a JSON list with both `llama3.1:8b` and
`nomic-embed-text` entries.

### 2. Sync project dependencies

```bash
cd /path/to/local-ai-server
uv sync
```

This creates a Python 3.12 venv under `.venv/` and installs the
runtime and dev dependencies declared in `pyproject.toml`.

### 3. (Optional) copy the example env file

```bash
cp config/.env.example config/.env
```

The defaults work out of the box. Edit `config/.env` only if you
want to override the gateway port, the registry path, the log level,
or the Ollama base URL. See [Configuration](#configuration) below
for the full list.

### 4. Start the gateway

```bash
uv run uvicorn app.main:app --port 8000
```

The gateway binds to `127.0.0.1:8000` by default. The lifespan logs
the loaded registry and the constructed adapters at INFO level:

```
INFO app.main registry loaded: 4 models (ollama-llama3, ollama-nomic-embed, mlx-mistral, model-runner-llama32); adapters: docker_model_runner, mlx, ollama
```

### 5. Verify

```bash
curl http://127.0.0.1:8000/healthz
# {"status":"ok"}

curl http://127.0.0.1:8000/v1/models | python -m json.tool
# {"object":"list","data":[ ... 4 entries ... ]}
```

---

## Common tasks

### List models

`curl`:

```bash
curl http://127.0.0.1:8000/v1/models | python -m json.tool
```

OpenAI Python SDK:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-used-yet",   # auth lands in v0.2.0
)

for m in client.models.list().data:
    print(m.id)
```

### Non-streaming chat

`curl`:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ollama-llama3",
    "messages": [
      {"role": "user", "content": "Say hi in three words."}
    ]
  }' | python -m json.tool
```

OpenAI SDK:

```python
r = client.chat.completions.create(
    model="ollama-llama3",
    messages=[
        {"role": "user", "content": "Say hi in three words."}
    ],
)
print(r.choices[0].message.content)
```

### Streaming chat

The gateway emits `text/event-stream` SSE; the OpenAI SDK consumes it
transparently as a chunk iterator. Each chunk's
`choices[0].delta.content` is a fragment of the assistant message;
concatenate them to assemble the full reply.

```python
stream = client.chat.completions.create(
    model="ollama-llama3",
    messages=[
        {"role": "user", "content": "Stream a haiku."}
    ],
    stream=True,
)

text = []
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        delta = chunk.choices[0].delta.content
        print(delta, end="", flush=True)
        text.append(delta)

assembled = "".join(text)
```

`curl` (use `-N` to disable output buffering so you see the chunks
arrive incrementally):

```bash
curl -sS -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ollama-llama3",
    "messages": [{"role": "user", "content": "Stream a haiku."}],
    "stream": true
  }'
```

The stream terminates with `data: [DONE]\n\n`; that line is
forwarded verbatim from Ollama.

### Embeddings

```python
e = client.embeddings.create(
    model="ollama-nomic-embed",
    input="hello world",
)
print("dim:", len(e.data[0].embedding))   # 768 for nomic-embed-text
```

Batched input also works:

```python
e = client.embeddings.create(
    model="ollama-nomic-embed",
    input=["hello", "world"],
)
print(len(e.data))                # 2
print(e.data[0].index, e.data[1].index)   # 0 1
```

### Adding a new model

Edit `config/models.yaml` and add an entry:

```yaml
models:
  # ... existing entries ...
  - id: ollama-qwen
    backend: ollama
    upstream_model: qwen2.5:7b
    capabilities: [chat, tools]
```

The registry is loaded **once** at startup in v0.1.0 — there is no
hot-reload (it lands with `watchfiles` in v0.2.0). Restart the
gateway to pick up changes:

```bash
# Ctrl+C the running uvicorn, then
uv run uvicorn app.main:app --port 8000
```

Required fields per entry:
- `id` — the registry-facing model id (what clients send in `model`).
- `backend` — one of `ollama`, `mlx`, `docker_model_runner`.
- `upstream_model` — the upstream backend's own model name.
- `capabilities` — a non-empty subset of `[chat, embeddings, tools]`.

Optional:
- `base_url` — per-model upstream base URL override.

Don't forget to `ollama pull <upstream_model>` for any new Ollama
entry before you call it through the gateway, otherwise Ollama will
return a 4xx that surfaces as a 500 from the gateway.

### Running tests

The test suite lives in `tests/` and is split into live and
non-live tests. Live tests carry the `live` marker and need a
running Ollama with both `llama3.1:8b` and `nomic-embed-text`
pulled; they auto-skip cleanly if Ollama isn't reachable.

```bash
# all tests; live tests auto-skip when Ollama isn't running
uv run pytest -q

# explicitly skip live tests (no Ollama prereq needed)
uv run pytest -q -m "not live"

# only the stub-adapter tests (no live deps at all)
uv run pytest -q tests/adapters/test_mlx_adapter.py \
                 tests/adapters/test_docker_model_runner_adapter.py
```

`pytest -n auto` (xdist parallelism) is **not recommended** — live
tests may flake under parallel execution against a single Ollama
process.

### Linting

```bash
uv run ruff check .
```

Should print `All checks passed!`.

---

## Configuration

All settings load via `pydantic-settings` from environment variables
or `config/.env` (see `app/config.py:8-34`).

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_HOST` | `127.0.0.1` | uvicorn bind host. v0.1.0 binds loopback only. |
| `GATEWAY_PORT` | `8000` | uvicorn bind port. |
| `MODELS_YAML_PATH` | `config/models.yaml` | Path to the model registry. |
| `LOG_LEVEL` | `INFO` | stdlib logging level (`DEBUG`/`INFO`/`WARNING`/`ERROR`). |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Upstream Ollama URL. |

Path resolution is relative to the process working directory; run
`uv run uvicorn ...` from the repo root so `config/models.yaml`
resolves correctly.

---

## Troubleshooting

### 501 with `code: backend_capability_missing`

**Symptom:**

```json
{"error": {"type":"not_supported", "code":"backend_capability_missing", ...}}
```

**Cause:** The model exists in the registry but doesn't declare the
capability the endpoint requires. For example, calling
`POST /v1/embeddings` with `model: "ollama-llama3"` returns this
error because `ollama-llama3` declares only `[chat, tools]`, not
`embeddings`.

**Fix:** Either call a different model, or add the missing
capability to the registry entry in `config/models.yaml` and
restart the gateway. Be sure the upstream backend actually supports
the capability — declaring `embeddings` on a chat-only model just
moves the error from a clean 501 to a confusing upstream 4xx
surfacing as a 500.

### 501 with `code: not_implemented`

**Symptom:**

```json
{"error": {"type":"not_supported", "code":"not_implemented", ...}}
```

**Cause:** The request reached the `MLXAdapter` or
`DockerModelRunnerAdapter` stub. v0.1.0 wires only the Ollama
backend; the other two raise `NotSupportedError` from every method.

**Fix:** Use a model whose `backend` is `ollama` in
`config/models.yaml`. Real MLX and Docker Model Runner support is
planned for v0.4.0+.

### 400 with `code: tools_not_supported`

**Symptom:**

```json
{"error": {"type":"invalid_request_error", "code":"tools_not_supported", "param":"tools", ...}}
```

**Cause:** Your request includes a `tools` array, but the model's
registry entry doesn't declare the `tools` capability. The gate
fires before any backend call, so the request never reaches Ollama.

**Fix:** Add `tools` to the model's `capabilities` list in
`config/models.yaml` (only do this if the upstream model genuinely
supports tool calls; see [`spec.md`](spec.md) §4.5 for the
cross-backend tool-call matrix), or omit the `tools` field from
the request.

### 404 with `code: model_not_found`

**Symptom:**

```json
{"error": {"type":"invalid_request_error", "code":"model_not_found", "param":"model", ...}}
```

**Cause:** The `model` field in your request doesn't match any `id`
in the registry. This is usually a typo or a missing registry entry.

**Fix:** Run `curl http://127.0.0.1:8000/v1/models` to see the
canonical ids; correct the request, or add the model to
`config/models.yaml` and restart.

### `uvicorn` won't start

**Symptom:** the process exits during boot with a
`RegistryError`, `FileNotFoundError`, or `ValidationError`
traceback.

**Causes and fixes:**

- **`FileNotFoundError: Registry file not found`** — `config/models.yaml`
  is missing or the path is wrong. Make sure you're running uvicorn
  from the repo root, or set `MODELS_YAML_PATH` explicitly.
- **`RegistryError: models[N]: missing 'X'`** — a registry entry is
  missing a required field (`id`, `backend`, `upstream_model`, or
  `capabilities`). Fix the YAML.
- **`RegistryError: models[N]: duplicate id 'Y'`** — two entries
  share an `id`. Make ids unique.
- **`RegistryError: models[N]: unknown capability 'Z'`** — only
  `chat`, `embeddings`, `tools` are recognized. Fix the YAML.
- **`ValueError: registry: unknown backend 'X'`** — `backend` must be
  one of `ollama`, `mlx`, `docker_model_runner`.
- **`pydantic.ValidationError`** — an env var has the wrong type
  (e.g., `GATEWAY_PORT=foo`). Fix the env var.

### Live tests fail with 404 from Ollama

**Symptom:** `pytest -q` runs but `tests/test_chat.py::test_chat_non_stream_live_ollama`
or similar fails with an Ollama 404 or a 500 from the gateway.

**Cause:** Ollama is running but the required models aren't pulled.

**Fix:**

```bash
ollama pull llama3.1:8b
ollama pull nomic-embed-text
```

Re-run pytest.

### Live tests skip when you expected them to run

**Symptom:** `pytest -q` reports tests as `skipped` instead of
`passed`, and you see something like:

```
SKIPPED [N] tests/test_chat.py: Ollama not reachable on :11434
```

**Cause:** The session-scoped `ollama_alive` probe in
`tests/conftest.py` couldn't reach `http://localhost:11434/api/tags`
within 2 seconds.

**Fix:** Make sure `ollama serve` is running and answering on the
default port. If Ollama is running on a non-default URL, the
auto-skip behavior protects you from a noisy test run; either
adjust your local Ollama or run the suite without the `live`
marker (`pytest -q -m "not live"`).

### Streaming response hangs or stalls mid-token

**Symptom:** The `text/event-stream` body delivers a few `data:`
lines and then nothing, or chunks arrive in big bursts instead of
incrementally.

**Cause and fix:** Most often this is an upstream network buffering
issue. Make sure your client respects SSE — `curl -sS -N` (the
`-N`/`--no-buffer` flag) is required to see chunks as they arrive.
The gateway already sets `Cache-Control: no-cache` and
`X-Accel-Buffering: no` to defeat reverse-proxy buffering. If you
have a transparent proxy in front of the gateway (you shouldn't in
v0.1.0), check its SSE handling.

### "uv sync" fails to resolve `>=3.12`

**Symptom:** `uv sync` reports it can't find a Python 3.12 toolchain.

**Cause:** The host doesn't have Python 3.12 installed and `uv`
hasn't downloaded its own toolchain yet.

**Fix:**

```bash
uv python install 3.12
uv sync
```

`uv` will provision an isolated 3.12 toolchain.

---

## Where to go next

- [`api-reference.md`](api-reference.md) — endpoint shapes, request
  fields, error envelope.
- [`architecture.md`](architecture.md) — components, request
  lifecycle, streaming contract, extension points.
- [`spec.md`](spec.md) — full v1 design (auth, Caddy, TLS, host
  Makefile, end-to-end verification).
- [`../examples/v0_1_0_demo.ipynb`](../examples/v0_1_0_demo.ipynb) —
  runnable end-to-end notebook.
