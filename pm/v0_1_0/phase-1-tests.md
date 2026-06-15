# Phase 1 — Manual Test Plan

Copy-pasteable commands to verify Phase 1 (Skeleton) before approving the merge.

Assumes you're at the repo root on `phase/local-ai-server/1-skeleton`.

> **Why every Python block uses `<<'PY' ... PY`** (single-quoted heredoc):
> bash and zsh do **zero** expansion inside a quoted heredoc — no `$VAR`, no
> backtick, no `!` history expansion. That keeps `f'{e!s}'`,
> `print(f"{x:>10}")`, and other shell-fragile syntax safe to paste verbatim.
> If you build your own probes with `python -c "..."` instead, watch for
> `!` triggering zsh history expansion.

```bash
cd /Users/ramikrispin/Personal/tutorials/local-ai-server
git branch --show-current
# expected: phase/local-ai-server/1-skeleton
```

Run **Tier 1** first. If anything in Tier 1 fails, stop and report — Tier 2/3 assume the basics work. Tier 2 covers what the sandbox QA tester could not verify. Tier 3 probes contract details.

---

## Tier 1 — Done-when checklist (architecture §11)

These are the four checks the architecture mandates pass before merge.

### 1. Install dependencies + lockfile

```bash
uv sync
```

Expected: success; `uv.lock` present; no resolution errors.

### 2. Import sanity — every module compiles, every public symbol is reachable

```bash
uv run python <<'PY'
import app.main, app.registry, app.schemas, app.errors
from app.adapters.base import BackendAdapter, NotSupportedError
from app.adapters.ollama import OllamaAdapter
from app.adapters.mlx import MLXAdapter
from app.adapters.docker_model_runner import DockerModelRunnerAdapter
print('imports ok')
PY
```

Expected: `imports ok`.

### 3. Registry loads with the 4 sample models

```bash
uv run python <<'PY'
from app.registry import load_registry
reg = load_registry('config/models.yaml')
print(sorted(m.id for m in reg.models))
PY
```

Expected: `['mlx-mistral', 'model-runner-llama32', 'ollama-llama3', 'ollama-nomic-embed']`.

### 4. Real socket boot + `/healthz` (the test the sandbox could not run)

```bash
uv run uvicorn app.main:app --port 8000 &
sleep 2
curl -sS -i http://127.0.0.1:8000/healthz
kill %1
wait %1 2>/dev/null
```

Expected:

- HTTP `200 OK`
- Body: `{"status":"ok"}`
- In uvicorn's stdout: a line containing `registry loaded: 4 models (...)`

If port 8000 is in use, replace with `--port 8765` (or any free port) and update the curl URL.

---

## Tier 2 — Real-host gaps

Things the in-sandbox QA tester could not fully exercise.

### 5. Env-var override flows through `Settings`

```bash
GATEWAY_PORT=9001 OLLAMA_BASE_URL=http://example.test:99 uv run python <<'PY'
from app.config import get_settings
s = get_settings()
print(s.gateway_port, s.ollama_base_url)
PY
```

Expected: `9001 http://example.test:99`.

(`get_settings` is `lru_cache`d per process; each invocation is a fresh process so the cache is irrelevant here.)

### 6. `config/.env` is picked up when present

```bash
cp config/.env.example config/.env
echo 'GATEWAY_PORT=9002' >> config/.env
uv run python <<'PY'
from app.config import get_settings
print(get_settings().gateway_port)
PY
rm config/.env
```

Expected: `9002`. (`config/.env` is intentionally not gitignored yet — Phase 3 adds that.)

### 7. `/healthz` via `TestClient` (in-process; bypasses the network entirely)

```bash
uv run python <<'PY'
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    r = c.get('/healthz')
    print(r.status_code, r.json())
PY
```

Expected: `200 {'status': 'ok'}`.

---

## Tier 3 — Contract probing (optional but informative)

Catches subtle issues the boot/health tests miss. Useful before Phase 2 plugs Ollama into the seam.

### 8. Stub adapters raise `NotSupportedError` with the correct codes

```bash
uv run python <<'PY'
import asyncio
from app.adapters.mlx import MLXAdapter
from app.adapters.docker_model_runner import DockerModelRunnerAdapter
from app.adapters.base import NotSupportedError

async def probe():
    for cls in (MLXAdapter, DockerModelRunnerAdapter):
        a = cls()
        try:
            await a.chat_completions({'model': 'x'}, False)
        except NotSupportedError as e:
            print(f'{cls.__name__}.chat -> code={e.code} backend={e.backend}')
        try:
            await a.embeddings({'model': 'x'})
        except NotSupportedError as e:
            print(f'{cls.__name__}.emb  -> code={e.code} backend={e.backend} param={e.param}')
        try:
            await a.health()
        except NotSupportedError as e:
            print(f'{cls.__name__}.hlth -> code={e.code} backend={e.backend}')

asyncio.run(probe())
PY
```

Expected:

```
MLXAdapter.chat -> code=not_implemented backend=mlx
MLXAdapter.emb  -> code=backend_capability_missing backend=mlx param=model
MLXAdapter.hlth -> code=not_implemented backend=mlx
DockerModelRunnerAdapter.chat -> code=not_implemented backend=docker_model_runner
DockerModelRunnerAdapter.emb  -> code=not_implemented backend=docker_model_runner param=None
DockerModelRunnerAdapter.hlth -> code=not_implemented backend=docker_model_runner
```

The `MLXAdapter.emb` line is the only one with `code=backend_capability_missing` — that's the architecture's deliberate "permanent capability gap" marker. Everything else is `not_implemented` (deferred-implementation).

### 9. `OllamaAdapter` raises `NotImplementedError`, NOT `NotSupportedError`

```bash
uv run python <<'PY'
import asyncio
from app.adapters.ollama import OllamaAdapter

async def probe():
    a = OllamaAdapter()
    try:
        await a.chat_completions({'model': 'x'}, False)
    except NotImplementedError as e:
        print(f'chat_completions: NotImplementedError({e})')
    try:
        await a.embeddings({'model': 'x'})
    except NotImplementedError as e:
        print(f'embeddings: NotImplementedError({e})')
    try:
        await a.health()
    except NotImplementedError as e:
        print(f'health: NotImplementedError({e})')

asyncio.run(probe())
PY
```

Expected:

```
chat_completions: NotImplementedError(filled in Phase 2)
embeddings: NotImplementedError(filled in Phase 2)
health: NotImplementedError(filled in Phase 2)
```

### 10. Schema passthrough — OpenAI vendor extras survive round-trip

```bash
uv run python <<'PY'
from app.schemas import ChatCompletionRequest

body = {
    'model': 'ollama-llama3',
    'messages': [{'role': 'user', 'content': 'hi'}],
    'stream': True,
    'reasoning_effort': 'medium',
    'metadata': {'tag': 'experimental'},
}
req = ChatCompletionRequest.model_validate(body)
out = req.model_dump(exclude_unset=True)
print('reasoning_effort kept:', 'reasoning_effort' in out)
print('metadata kept:', 'metadata' in out)
print('stream:', out['stream'])
PY
```

Expected:

```
reasoning_effort kept: True
metadata kept: True
stream: True
```

This confirms `extra='allow'` on `_OpenAIModel` is doing what spec §4.2 requires (vendor-extension passthrough).

### 11. Registry validation — duplicate id raises `RegistryError`

```bash
uv run python <<'PY'
import tempfile, pathlib
from app.registry import load_registry, RegistryError

bad = '''models:
  - id: dup
    backend: ollama
    upstream_model: x
    capabilities: [chat]
  - id: dup
    backend: ollama
    upstream_model: y
    capabilities: [chat]
'''
p = pathlib.Path(tempfile.mkstemp(suffix='.yaml')[1])
p.write_text(bad)
try:
    load_registry(p)
    print('FAIL: no error raised')
except RegistryError as e:
    print(f'caught: {e}')
finally:
    p.unlink()
PY
```

Expected: a `caught:` line mentioning duplicate id `dup`.

### 12. Registry validation — unknown capability raises `RegistryError`

```bash
uv run python <<'PY'
import tempfile, pathlib
from app.registry import load_registry, RegistryError

bad = '''models:
  - id: x
    backend: ollama
    upstream_model: y
    capabilities: [chat, vision]
'''
p = pathlib.Path(tempfile.mkstemp(suffix='.yaml')[1])
p.write_text(bad)
try:
    load_registry(p)
    print('FAIL: no error raised')
except RegistryError as e:
    print(f'caught: {e}')
finally:
    p.unlink()
PY
```

Expected: a `caught:` line mentioning the unknown capability `vision`.

### 13. Lint (isolated mode)

Project-mode `ruff check .` is blocked by the pre-existing `ruff.toml` typo (`quite-style`); the architecture explicitly defers fixing that to Phase 3. Use isolated mode in the meantime:

```bash
uv run ruff check app/ --isolated --line-length 79
```

Expected: `All checks passed!`.

---

## After testing

If everything passed, come back with `/pm` and pick **A — Approve merge** at the Pre-Merge Validation gate. Phase 1 lands on `dev/local-ai-server` and Phase 2 (Endpoints + Ollama wiring) starts.

If anything failed, capture the failing command's output and pick **C — Request changes** at the same gate; the Fixer Agent will scope the repair against the architecture spec.
