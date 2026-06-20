# Usage Guide

**Project**: local-ai-server
**Version**: v0.2.0

A how-to companion to the [API reference](api-reference.md) and the
[architecture overview](architecture.md). The README's quick-start is
the minimal copy-pasteable boot sequence; this guide walks through the
same flow in longer-form prose plus the workflows the README skips
(`/readyz` probing, hot-reload demo, key revocation, test suite, where
to look in the code).

---

## Mental model

Every request flows through three layers:

1. **Middleware stack** — added in `app/main.py:create_app()`. From
   outermost to innermost on a `/v1/*` request:
   `RequestLoggingMiddleware` (times the call, captures fields) →
   `BearerAuthMiddleware` (verifies the `sk-local-...` token against
   `data/keys.db`) → router.
2. **Router** (`app/routers/`) — validates the request body against
   a Pydantic schema, looks up the model in the registry, runs the
   capability and tools gates, then dispatches.
3. **Adapter** (`app/adapters/`) — an async `httpx` client that
   forwards the request to the upstream backend (Ollama for v0.2.0;
   MLX and Docker Model Runner are 501 stubs).

`/healthz` and `/readyz` bypass auth via the `PUBLIC_PATHS` allowlist
in `app/auth.py:138-146` but still flow through the logging middleware
(with `key_prefix=null` because auth was skipped).

When a request comes in, the registry-facing model id is rewritten
to the upstream model name before the adapter call, so Ollama
receives `llama3.1:8b`, not `ollama-llama3`.

For the full lifecycle and seam diagrams see
[`architecture.md`](architecture.md).

---

## Workflow 1: First-time setup

### Step 1: install Ollama and pull the test models

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

### Step 2: sync project dependencies

```bash
cd /path/to/local-ai-server
uv sync
```

This creates a Python 3.12 venv under `.venv/` and installs the
runtime and dev dependencies declared in `pyproject.toml`. The v0.2.0
runtime stack adds three packages on top of v0.1.0: `argon2-cffi`
(key hashing), `structlog` (JSON logging), `watchfiles` (registry
hot-reload).

### Step 3: (optional) copy the example env file

```bash
cp config/.env.example config/.env
```

The defaults work out of the box. Edit `config/.env` only if you
want to override the gateway port, the registry path, the log level,
the Ollama base URL, or the SQLite key store path. See
[Configuration](#configuration) below for the full list.

---

## Workflow 2: Mint your first API key

### Step 1: ensure the data directory exists

The default key store lives at `./data/keys.db` (override via
`KEYS_DB_PATH`). The mint script auto-creates the parent directory
and the SQLite table on first run, so this step is purely defensive
on a fresh checkout:

```bash
mkdir -p data
```

### Step 2: run the mint script

```bash
uv run python scripts/generate_api_key.py --name my-laptop
```

The script:

1. Generates `sk-local-` + 32 URL-safe random bytes
   (`secrets.token_urlsafe(32)`).
2. Hashes it with Argon2id (`argon2.PasswordHasher().hash(token)`).
3. Inserts a row into `api_keys` with `prefix = token[:12]`,
   `name = "my-laptop"`, `created_at = now`.
4. Prints the plaintext token on stdout, **exactly once**. A short
   courtesy line goes to stderr.

**Expected stdout** (one line, captured into a shell variable below):

```
sk-local-<43-char-urlsafe-token>
```

The plaintext is never persisted — only the Argon2id hash is. If you
lose the token, revoke the prefix and mint a new one; you cannot
recover the plaintext.

### Step 3: capture the key into a variable

The standard idiom (matches what the test scripts and the README
quickstart use):

```bash
KEY=$(uv run python scripts/generate_api_key.py --name my-laptop \
      | grep -oE 'sk-local-[A-Za-z0-9_-]+')
echo "Save this key: $KEY"
```

For repeated use across shell sessions, save it to your shell
profile or a secret manager. A simple workflow for local
development:

```bash
echo "export LOCAL_AI_KEY=$KEY" >> ~/.zshrc.local
```

### Step 4: verify the row landed

```bash
uv run python -c "
import sqlite3
c = sqlite3.connect('data/keys.db')
print(c.execute('SELECT prefix, name, revoked_at FROM api_keys').fetchall())
"
```

Expected:

```
[('sk-local-XXX', 'my-laptop', None)]
```

The `prefix` is the first 12 characters of the plaintext (so
`sk-local-` + 3 random chars). `revoked_at` is `NULL` because the key
is live.

---

## Workflow 3: Boot the gateway

```bash
uv run uvicorn app.main:app --port 8000
```

The gateway binds to `127.0.0.1:8000` by default. On startup, the
lifespan logs one JSON line per event:

```json
{"path": "config/models.yaml", "event": "registry_watcher_started", "level": "info", "ts": "2026-06-20T10:11:12.345Z"}
{"n_models": 4, "ids": ["ollama-llama3", "ollama-nomic-embed", "mlx-mistral", "model-runner-llama32"], "adapters": ["docker_model_runner", "mlx", "ollama"], "event": "registry_loaded", "level": "info", "ts": "2026-06-20T10:11:12.367Z"}
```

(Field order varies; the `event` key is what to grep for.)

Every gateway log line on stdout is parseable JSON. The watcher task
starts in the same lifespan startup and runs for the lifetime of the
process.

---

## Workflow 4: Make an authenticated request

### Step 1: curl probe

```bash
curl -sS -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:8000/v1/models | python -m json.tool
```

Expected: a JSON object with `"object": "list"` and 4 entries in
`data`. The `model.id` values match the registry: `ollama-llama3`,
`ollama-nomic-embed`, `mlx-mistral`, `model-runner-llama32`.

If you omit the header or pass a malformed/unknown/revoked token, you
get HTTP 401 with the OpenAI envelope:

```bash
curl -sS http://127.0.0.1:8000/v1/models | python -m json.tool
```

```json
{
  "error": {
    "type": "invalid_request_error",
    "message": "Missing Authorization header",
    "param": "Authorization",
    "code": "invalid_api_key"
  }
}
```

### Step 2: OpenAI Python SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key=os.environ["LOCAL_AI_KEY"],   # export LOCAL_AI_KEY=sk-local-...
)

# List models
for m in client.models.list().data:
    print(m.id)
```

The SDK accepts the key as `api_key="sk-local-..."` — no other code
changes vs. calling api.openai.com. Every method
(`client.chat.completions.create(...)`,
`client.embeddings.create(...)`, `client.models.list()`) sends the
bearer token automatically.

### Step 3: non-streaming chat

```python
r = client.chat.completions.create(
    model="ollama-llama3",
    messages=[{"role": "user", "content": "Say hi in three words."}],
)
print(r.choices[0].message.content)
```

`curl` equivalent:

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ollama-llama3",
    "messages": [
      {"role": "user", "content": "Say hi in three words."}
    ]
  }' | python -m json.tool
```

### Step 4: streaming chat

The gateway emits `text/event-stream` SSE; the OpenAI SDK consumes
it transparently as a chunk iterator. Each chunk's
`choices[0].delta.content` is a fragment of the assistant message;
concatenate them to assemble the full reply.

```python
stream = client.chat.completions.create(
    model="ollama-llama3",
    messages=[{"role": "user", "content": "Stream a haiku."}],
    stream=True,
)

for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
print()
```

`curl` with `-N` to disable output buffering so you see the chunks
arrive incrementally:

```bash
curl -sS -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "ollama-llama3",
    "messages": [{"role": "user", "content": "Stream a haiku."}],
    "stream": true
  }'
```

The stream terminates with `data: [DONE]\n\n`; that line is
forwarded verbatim from Ollama.

### Step 5: embeddings

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
print(len(e.data))                          # 2
print(e.data[0].index, e.data[1].index)     # 0 1
```

---

## Workflow 5: Probe `/readyz`

`/readyz` is public (no auth required). It fans out concurrently over
every adapter's `health()` method and returns:

- **200** with `{"status": "ok", "backends": {...}}` if at least one
  backend reports `status == "ok"`.
- **503** with the OpenAI envelope plus a `backends` extension if
  every backend is unreachable or errored.

### Happy path (Ollama up)

```bash
curl -sS http://127.0.0.1:8000/readyz | python -m json.tool
```

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

The MLX and Docker Model Runner stubs always show `"status": "error"`
in v0.2.0 — they raise `NotSupportedError` from their `health()`
methods, and the route's `asyncio.gather(return_exceptions=True)`
maps that to the uniform `{"status": "error", "error": "..."}`
payload. The top-level `status` is `"ok"` because Ollama is up.

### Degraded path (Ollama down)

Stop Ollama (e.g. `pkill ollama` or point `OLLAMA_BASE_URL` at a
non-listening port and restart the gateway) and retry:

```bash
curl -sS http://127.0.0.1:8000/readyz | python -m json.tool
```

```json
{
  "error": {
    "type": "service_unavailable",
    "message": "No backends reachable",
    "param": null,
    "code": "no_backends_reachable"
  },
  "backends": {
    "docker_model_runner": {"status": "error", "error": "NotSupportedError: ..."},
    "mlx": {"status": "error", "error": "NotSupportedError: ..."},
    "ollama": {"status": "unreachable", "error": "All connection attempts failed"}
  }
}
```

HTTP status is 503. The `error` block matches the OpenAI envelope
shape exactly; the top-level `backends` key is a v0.2.0 spec
extension for operational debugging and is ignored by SDK clients.

### Expected result

In normal operation (Ollama running) `/readyz` should return 200.
Use it as a Kubernetes-style readiness probe or as a manual health
check before pointing a script at the gateway.

---

## Workflow 6: Hot-reload `config/models.yaml`

The gateway watches `MODELS_YAML_PATH` via `watchfiles`. Every save
triggers an attempt to reload the registry; on success, the new
`Registry` instance atomically replaces `app.state.registry` via a
single reference swap. In-flight requests continue to use the
snapshot they entered with; subsequent requests see the new
registry. The process is **not** restarted.

### Step 1: capture the current model list

```bash
curl -sS -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:8000/v1/models \
  | python -c "import sys,json; print([m['id'] for m in json.load(sys.stdin)['data']])"
```

Expected (the canonical v0.2.0 registry):

```
['ollama-llama3', 'ollama-nomic-embed', 'mlx-mistral', 'model-runner-llama32']
```

### Step 2: edit `config/models.yaml`

Add a new entry. Any text editor or the inline Python below works:

```bash
python -c "
import yaml
p = 'config/models.yaml'
d = yaml.safe_load(open(p))
d['models'].append({
    'id': 'hot-reload-canary',
    'backend': 'ollama',
    'upstream_model': 'llama3.1:8b',
    'capabilities': ['chat'],
})
open(p, 'w').write(yaml.safe_dump(d))
"
```

### Step 3: observe the gateway picking it up

`watchfiles` debounces filesystem events at 50 ms; allow ~1 s for
the reload to complete:

```bash
sleep 1.5
curl -sS -H "Authorization: Bearer $KEY" \
  http://127.0.0.1:8000/v1/models \
  | python -c "import sys,json; print([m['id'] for m in json.load(sys.stdin)['data']])"
```

Expected:

```
['ollama-llama3', 'ollama-nomic-embed', 'mlx-mistral', 'model-runner-llama32', 'hot-reload-canary']
```

In the gateway's stdout, look for a `registry_reloaded` event:

```json
{"path": "config/models.yaml", "n_models": 5, "old_ids": [...], "new_ids": [..., "hot-reload-canary"], "event": "registry_reloaded", ...}
```

### Limitations to know

- **New backend types require a restart.** Adapters are constructed
  once at lifespan startup. A model whose `backend` is already
  represented in `app.state.adapters` (e.g. `ollama`) works hot;
  adding the first-ever `mlx` entry on a deployment that booted
  without any MLX models would `KeyError` on the next request to
  that model. Adapter rebuild lands in v0.4.0+.
- **Malformed YAML is safe.** If a save produces invalid YAML, the
  watcher logs `event="registry_reload_failed"` with a `reason`
  discriminator and keeps the previous registry live. The gateway
  never crashes because of a bad edit.
- **`base_url` changes don't propagate to existing adapters.** An
  adapter is built with its `httpx.AsyncClient` URL fixed at startup;
  editing a model's `base_url` in YAML changes the registry record
  but the adapter still talks to the old URL until restart.

### Expected result

The new `hot-reload-canary` id appears in `/v1/models` within ~1 s
of the file save, with no process restart and no error in the
gateway logs. Restore the original `config/models.yaml` (remove the
canary entry) when you're done experimenting.

---

## Workflow 7: Revoke a key

When a key is compromised or no longer needed, revoke it. Revocation
sets `revoked_at = now` on the row; subsequent requests using that
token return 401 with `message="Invalid API key"`. The plaintext is
not deletable — revocation is the disable mechanism.

### Step 1: identify the prefix

The prefix is the first 12 characters of the plaintext token (e.g.
`sk-local-AbC` for a token `sk-local-AbCdEf...`). If you no longer
have the plaintext, list known prefixes:

```bash
sqlite3 data/keys.db \
  "SELECT prefix, name, datetime(created_at, 'unixepoch'), \
          datetime(last_used_at, 'unixepoch'), revoked_at \
   FROM api_keys"
```

### Step 2: run the revoke script

```bash
uv run python scripts/revoke_api_key.py --prefix sk-local-AbC
```

Exit code `0` and a `revoked: sk-local-AbC` line on stdout indicates
success. Exit code `1` with `unknown or already-revoked prefix` on
stderr indicates either no such prefix or the prefix was previously
revoked (the operation is idempotent — only the first call returns
true because the SQL `UPDATE` requires `revoked_at IS NULL`).

### Step 3: verify

```bash
curl -sS -H "Authorization: Bearer $KEY" \
     http://127.0.0.1:8000/v1/models
```

Expected: 401 with `code="invalid_api_key"`. The middleware does not
distinguish unknown-prefix from revoked-key in the response body
(OWASP-style: don't leak whether a prefix exists). The distinction
is visible in the SQLite row's `revoked_at` column.

### Expected result

The key is dead from the gateway's perspective. To restore access,
mint a new key with `generate_api_key.py`. There is no un-revoke
operation by design.

---

## Workflow 8: Run the test suite

The test suite lives in `tests/` and is split into live and
non-live tests. Live tests carry the `live` marker and need a
running Ollama with both `llama3.1:8b` and `nomic-embed-text`
pulled; they auto-skip cleanly if Ollama isn't reachable.

Auth tests use a temp SQLite key store (`temp_keys_db` fixture in
`tests/conftest.py`); production keys are never touched. The test
fixture also lowers Argon2 parameters (`time_cost=1, memory_cost=8,
parallelism=1`) so the auth suite finishes in <100 ms of crypto
work. Verification still uses the runtime `PasswordHasher()` because
Argon2id stores its parameters in the hash string — the runtime
verifier reads them from the hash and runs at the parameters the
hash was created with.

```bash
# all tests; live tests auto-skip when Ollama isn't running
uv run pytest -q

# explicitly skip live tests (no Ollama prereq needed)
uv run pytest -q -m "not live"

# the v0.2.0 surfaces in isolation
uv run pytest -q tests/test_auth.py tests/test_keys_db.py \
                 tests/test_readyz.py tests/test_hot_reload.py \
                 tests/test_logging.py
```

`pytest -n auto` (xdist parallelism) is **not recommended** — live
tests may flake under parallel execution against a single Ollama
process.

### Type and lint checks

```bash
uv run mypy app/ --strict
uv run ruff check .
```

Both should pass with zero errors / "All checks passed!".

---

## Configuration

All settings load via `pydantic-settings` from environment variables
or `config/.env` (see `app/config.py:8-44`).

| Variable | Default | Purpose |
|---|---|---|
| `GATEWAY_HOST` | `127.0.0.1` | uvicorn bind host. v0.2.0 binds loopback only; LAN binding lands in v0.3.0 with Caddy. |
| `GATEWAY_PORT` | `8000` | uvicorn bind port. |
| `MODELS_YAML_PATH` | `config/models.yaml` | Path to the model registry. Watched by `watchfiles` in v0.2.0. |
| `LOG_LEVEL` | `INFO` | Log level fed to `configure_structlog(...)`. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Upstream Ollama URL. |
| `KEYS_DB_PATH` | `./data/keys.db` | **NEW in v0.2.0.** SQLite key store path. |

Path resolution is relative to the process working directory; run
`uv run uvicorn ...` from the repo root so `config/models.yaml` and
`data/keys.db` resolve correctly.

---

## Troubleshooting

### 401 with `code: invalid_api_key`

**Symptom:**

```json
{"error": {"type":"invalid_request_error", "code":"invalid_api_key", "param":"Authorization", "message":"..."}}
```

**Causes (the `message` field distinguishes them):**

- `Missing Authorization header` — no `Authorization` header on the
  request. Add `-H "Authorization: Bearer $KEY"` to curl, or pass
  `api_key="sk-local-..."` to the OpenAI SDK.
- `Malformed Authorization header` — header present but doesn't
  match `Bearer <token>`. Common mistakes: wrong scheme
  (`Token sk-local-...`, `Basic ...`), missing space, empty token
  after `Bearer`.
- `Invalid API key` — the token's 12-char prefix is not in
  `data/keys.db` (or the row exists but `revoked_at IS NOT NULL`,
  or the Argon2 verify failed). Either you typo'd the token, the
  key was revoked, or you're hitting a different DB than the one
  the gateway is configured to use.

**Fix**:

```bash
# Confirm which DB the gateway is using
grep -E '^KEYS_DB_PATH' config/.env || echo "default: ./data/keys.db"

# List known prefixes (live + revoked)
sqlite3 data/keys.db \
  "SELECT prefix, name, revoked_at FROM api_keys"

# Mint a fresh key if the old one is gone
uv run python scripts/generate_api_key.py --name $(whoami)-debug
```

### `KEYS_DB_PATH` points to a missing parent directory

**Symptom:** the mint script (or any auth-middleware call) raises
`OSError: [Errno 30] Read-only file system` or similar at first run.

**Cause:** `_init_db` tries to create the parent directory via
`db_path.parent.mkdir(parents=True, exist_ok=True)`. If the path is
on a read-only or unwritable mount, the mkdir fails.

**Fix:** point `KEYS_DB_PATH` somewhere writable, e.g.
`KEYS_DB_PATH=$HOME/.local/share/local-ai-server/keys.db`. The
script will create the directory on its next run.

### Hot-reload didn't pick up my edit

**Symptom:** edited `config/models.yaml`, but `/v1/models` still
shows the old list after a few seconds.

**Causes:**

- The gateway is watching a different file than the one you edited.
  Check `MODELS_YAML_PATH` in `config/.env` and confirm it resolves
  to the file you're editing (paths are relative to the gateway's
  CWD).
- The YAML save produced a malformed file. Look in the gateway's
  stdout for a `registry_reload_failed` event with a `reason` like
  `parse_error`. Fix the YAML and save again; the previous (valid)
  registry stays live in the meantime.
- You added a model whose `backend` is `mlx` or `docker_model_runner`
  but no model with that backend was present at startup. The
  registry update succeeds, but the adapter doesn't exist; requests
  to the new model id will surface as 500s from the gateway.
  Restart to rebuild adapters from the new registry.

**Fix:** check the gateway logs first; the watcher emits one event
per save (`registry_reloaded`, `registry_reloaded_unchanged`, or
`registry_reload_failed`).

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
capability to the registry entry in `config/models.yaml` (and let
hot-reload pick it up). Be sure the upstream backend actually
supports the capability — declaring `embeddings` on a chat-only
model just moves the error from a clean 501 to a confusing upstream
4xx surfacing as a 500.

### 501 with `code: not_implemented`

**Symptom:**

```json
{"error": {"type":"not_supported", "code":"not_implemented", ...}}
```

**Cause:** The request reached the `MLXAdapter` or
`DockerModelRunnerAdapter` stub. v0.2.0 wires only the Ollama
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
in the registry.

**Fix:** Hit `GET /v1/models` (with auth) to see the canonical ids;
correct the request, or add the model to `config/models.yaml`.

### `uvicorn` won't start

**Symptom:** the process exits during boot with a `RegistryError`,
`FileNotFoundError`, or `ValidationError` traceback.

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
default port. Live tests in `tests/test_readyz.py` skip via the
same probe.

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
v0.2.0), check its SSE handling.

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
  fields, the 401 envelope, structured log field schema, capability
  decision tables.
- [`architecture.md`](architecture.md) — components, middleware
  install order, key store discipline, hot-reload swap discipline,
  `/readyz` aggregation rules, design decisions, extension points.
- [`spec.md`](spec.md) — long-term v1 design (Caddy, TLS, container
  setup, host Makefile, end-to-end verification plan).
- [`../posts/v0_2_0_auth_demo.ipynb`](../posts/v0_2_0_auth_demo.ipynb) —
  runnable notebook: mint a key, make an authenticated SDK call,
  show the 401 path.
- [`../posts/v0_2_0_readyz_hotreload_demo.ipynb`](../posts/v0_2_0_readyz_hotreload_demo.ipynb) —
  runnable notebook: `/readyz` probing plus the hot-reload demo.
