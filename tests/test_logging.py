"""Tests for the structlog logging contract.

Covers redaction, per-request event shape, startup events, and the
deliberate-leak defense.

Module docstring / fixture ordering note:
  When a test asserts on lifespan startup events (e.g.
  test_logging_registry_loaded_event_emitted_on_startup), `captured_log`
  MUST appear BEFORE `client_with_auth` in the test's argument list so
  pytest evaluates `captured_log` first. That fixture monkeypatches
  `app.logging.configure_structlog` to a no-op shim BEFORE the lifespan
  startup calls it, ensuring our capture chain is not clobbered.

Test inventory (17 tests):
  - test_logging_redact_authorization_key_match
  - test_logging_redact_authorization_value_match
  - test_logging_redact_authorization_case_insensitive_key
  - test_logging_redact_authorization_case_insensitive_scheme
  - test_logging_redact_authorization_short_token_falls_back_to_redacted
  - test_logging_redact_authorization_non_string_value_under_authorization_key
  - test_logging_redact_authorization_does_not_walk_nested
  - test_logging_request_event_emits_required_fields
  - test_logging_request_event_carries_key_prefix_12_chars
  - test_logging_request_event_for_chat_completions_carries_model_backend
  - test_logging_streaming_request_event_emits_null_usage
  - test_logging_no_bearer_substring_in_captured_events
  - test_logging_deliberate_leak_attempt_redacts
  - test_logging_registry_loaded_event_emitted_on_startup
  - test_logging_invalid_request_validation_carries_key_prefix
  - test_logging_configure_structlog_sets_stdout_handler
  - test_logging_configure_structlog_unknown_level_falls_back_to_info
"""
import json
import logging as stdlib_logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from pytest_httpx import HTTPXMock

from app.logging import _scrub, configure_structlog, redact_authorization


# ---------------------------------------------------------------------------
# _scrub / redact_authorization unit tests (no fixtures needed)
# ---------------------------------------------------------------------------


def test_logging_redact_authorization_key_match() -> None:
    """Key matching 'authorization' has its value scrubbed."""
    event_dict: dict[str, Any] = {
        "authorization": "Bearer sk-local-AbCdEf123456"
    }
    result = redact_authorization(None, "info", event_dict)
    # PREFIX_LEN=12: "sk-local-AbC" is the first 12 chars of the token.
    assert result["authorization"] == "<redacted: sk-local-AbC>"
    assert "sk-local-AbCdEf" not in repr(result)


def test_logging_redact_authorization_value_match() -> None:
    """Any field whose string value starts with 'Bearer ' is scrubbed."""
    event_dict: dict[str, Any] = {
        "some_field": "Bearer sk-local-AbCdEf123456"
    }
    result = redact_authorization(None, "info", event_dict)
    # PREFIX_LEN=12: token[:12] = "sk-local-AbC"
    assert result["some_field"] == "<redacted: sk-local-AbC>"


def test_logging_redact_authorization_case_insensitive_key() -> None:
    """Key matching is case-insensitive for the 'authorization' key."""
    for key in ("Authorization", "AUTHORIZATION", "authorization", "aUtHoRiZaTiOn"):
        event_dict: dict[str, Any] = {key: "Bearer sk-local-Xyz12345678"}
        result = redact_authorization(None, "info", event_dict)
        assert result[key].startswith("<redacted"), (
            f"Key '{key}' was not redacted"
        )


def test_logging_redact_authorization_case_insensitive_scheme() -> None:
    """Value-match redaction is case-insensitive on the 'Bearer ' scheme."""
    for scheme in ("Bearer ", "bearer ", "BEARER ", "BeArEr "):
        value = f"{scheme}sk-local-SomeToken123"
        event_dict: dict[str, Any] = {"header": value}
        result = redact_authorization(None, "info", event_dict)
        assert result["header"].startswith("<redacted"), (
            f"Scheme '{scheme}' was not redacted"
        )


def test_logging_redact_authorization_short_token_falls_back_to_redacted() -> None:
    """_scrub returns '<redacted>' when the token is shorter than PREFIX_LEN."""
    result = _scrub("Bearer x")
    assert result == "<redacted>"


def test_logging_redact_authorization_non_string_value_under_authorization_key() -> None:
    """Non-string value under 'authorization' key is scrubbed generically."""
    event_dict: dict[str, Any] = {"authorization": None}
    result = redact_authorization(None, "info", event_dict)
    assert result["authorization"] == "<redacted>"


def test_logging_redact_authorization_does_not_walk_nested() -> None:
    """Nested dicts are NOT walked; top-level-only in v0.2.0."""
    event_dict: dict[str, Any] = {
        "headers": {"Authorization": "Bearer sk-local-NestedLeak12"}
    }
    result = redact_authorization(None, "info", event_dict)
    nested = result["headers"]
    # The nested dict should be unchanged.
    assert isinstance(nested, dict)
    assert nested["Authorization"] == "Bearer sk-local-NestedLeak12"


# ---------------------------------------------------------------------------
# Request event shape (TestClient-level)
# ---------------------------------------------------------------------------


def test_logging_request_event_emits_required_fields(
    captured_log: list[dict[str, Any]],
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """GET /v1/models emits a 'request' event with all 10 required fields."""
    client_with_auth.get("/v1/models", headers=auth_headers)
    request_events = [e for e in captured_log if e.get("event") == "request"]
    assert request_events, "No 'request' event captured"
    evt = request_events[-1]
    required = (
        "path",
        "method",
        "status",
        "latency_ms",
        "key_prefix",
        "model",
        "backend",
        "stream",
        "prompt_tokens",
        "completion_tokens",
    )
    for field in required:
        assert field in evt, f"Missing required field '{field}' in request event"


def test_logging_request_event_carries_key_prefix_12_chars(
    captured_log: list[dict[str, Any]],
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """The request event's key_prefix equals the 12-char prefix of the key."""
    _db, _plaintext, expected_prefix = temp_keys_db
    client_with_auth.get("/v1/models", headers=auth_headers)
    request_events = [e for e in captured_log if e.get("event") == "request"]
    assert request_events
    evt = request_events[-1]
    assert evt.get("key_prefix") == expected_prefix
    assert len(expected_prefix) == 12


def test_logging_request_event_for_chat_completions_carries_model_backend(
    captured_log: list[dict[str, Any]],
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
    httpx_mock: HTTPXMock,
) -> None:
    """POST /v1/chat/completions emits request event with model and backend."""
    fake_response = json.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Hi"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 1,
                "total_tokens": 6,
            },
        }
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:11434/v1/chat/completions",
        content=fake_response.encode(),
        headers={"content-type": "application/json"},
    )
    client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ollama-llama3",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    request_events = [e for e in captured_log if e.get("event") == "request"]
    assert request_events
    evt = request_events[-1]
    assert evt.get("model") == "ollama-llama3"
    assert evt.get("backend") == "ollama"
    assert evt.get("stream") is False
    assert evt.get("status") == 200
    assert isinstance(evt.get("latency_ms"), int)
    assert evt["latency_ms"] >= 0


def test_logging_streaming_request_event_emits_null_usage(
    captured_log: list[dict[str, Any]],
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
    httpx_mock: HTTPXMock,
) -> None:
    """Streaming response without usage block yields null token counts."""
    # SSE chunks without a usage field.
    sse_body = (
        b'data: {"choices":[{"delta":{"content":"Hello"},"index":0}]}\n\n'
        b"data: [DONE]\n\n"
    )
    httpx_mock.add_response(
        method="POST",
        url="http://localhost:11434/v1/chat/completions",
        content=sse_body,
        headers={"content-type": "text/event-stream"},
    )
    client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={
            "model": "ollama-llama3",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    request_events = [e for e in captured_log if e.get("event") == "request"]
    assert request_events
    evt = request_events[-1]
    assert evt.get("prompt_tokens") is None
    assert evt.get("completion_tokens") is None


# ---------------------------------------------------------------------------
# Redaction grep
# ---------------------------------------------------------------------------


def test_logging_no_bearer_substring_in_captured_events(
    captured_log: list[dict[str, Any]],
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
) -> None:
    """No captured event contains 'Bearer ' followed by a token fragment."""
    client_with_auth.get("/v1/models", headers=auth_headers)
    serialized = json.dumps(captured_log, default=str)
    # The full token must never appear; only <redacted: sk-local-...> is OK.
    from tests.conftest import _TEST_KEY_PLAINTEXT_BASE

    full_token = _TEST_KEY_PLAINTEXT_BASE
    assert full_token not in serialized, (
        "Full token plaintext found in captured log events"
    )


def test_logging_deliberate_leak_attempt_redacts(
    captured_log: list[dict[str, Any]],
) -> None:
    """A structlog event with a Bearer value under 'authorization' is scrubbed.

    This is the deliberate-leak test: even if a developer adds a log call
    with a plaintext token, the redaction processor catches it.
    """
    structlog.get_logger("test").info(
        "leaktest",
        authorization="Bearer sk-local-LeakAttempt9876",
        header="Bearer sk-local-Other1234567",
    )
    leak_events = [e for e in captured_log if e.get("event") == "leaktest"]
    assert leak_events, "leaktest event was not captured"
    evt = leak_events[-1]
    # Both fields must be redacted to the '<redacted' prefix form.
    auth_val = evt.get("authorization", "")
    header_val = evt.get("header", "")
    assert str(auth_val).startswith("<redacted"), (
        f"authorization not redacted: {auth_val}"
    )
    assert str(header_val).startswith("<redacted"), (
        f"header not redacted: {header_val}"
    )


# ---------------------------------------------------------------------------
# Startup event regression
# ---------------------------------------------------------------------------


def test_logging_registry_loaded_event_emitted_on_startup(
    captured_log: list[dict[str, Any]],
    client_with_auth: TestClient,
) -> None:
    """Lifespan startup emits event='registry_loaded' with n_models=4."""
    startup_events = [
        e for e in captured_log if e.get("event") == "registry_loaded"
    ]
    assert startup_events, (
        "No 'registry_loaded' event captured during lifespan startup"
    )
    evt = startup_events[-1]
    assert evt.get("n_models") == 4
    assert isinstance(evt.get("ids"), list)
    assert len(evt["ids"]) == 4
    assert isinstance(evt.get("adapters"), list)
    assert len(evt["adapters"]) == 3


# ---------------------------------------------------------------------------
# Validation-path key_prefix lock
# ---------------------------------------------------------------------------


def test_logging_invalid_request_validation_carries_key_prefix(
    captured_log: list[dict[str, Any]],
    client_with_auth: TestClient,
    auth_headers: dict[str, str],
    temp_keys_db: tuple[Path, str, str],
) -> None:
    """A 422 from RequestValidationError still carries key_prefix in the log.

    Auth runs first (setting key_prefix on scope state), then validation
    fires. The logging middleware reads key_prefix in its finally block,
    so it sees the prefix even on rejected requests.
    """
    _db, _plaintext, expected_prefix = temp_keys_db
    client_with_auth.post(
        "/v1/chat/completions",
        headers=auth_headers,
        json={"model": "ollama-llama3"},  # missing 'messages' — triggers 422
    )
    request_events = [e for e in captured_log if e.get("event") == "request"]
    assert request_events
    evt = request_events[-1]
    assert evt.get("status") == 422
    assert evt.get("key_prefix") == expected_prefix


# ---------------------------------------------------------------------------
# configure_structlog contract
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_root_logger() -> Iterator[None]:
    """Snapshot and restore the stdlib root logger level and handlers.

    Applied only to tests that call configure_structlog() directly, which
    mutates the root logger. Scoped to individual tests via opt-in.
    """
    root = stdlib_logging.getLogger()
    saved_level = root.level
    saved_handlers = root.handlers[:]
    yield
    root.setLevel(saved_level)
    root.handlers = saved_handlers


def test_logging_configure_structlog_sets_stdout_handler(
    restore_root_logger: None,
) -> None:
    """After configure_structlog('INFO'), root logger has a stdout handler."""
    configure_structlog("INFO")
    root = stdlib_logging.getLogger()
    assert any(
        getattr(h, "stream", None) is sys.stdout for h in root.handlers
    ), "No stdout StreamHandler on root logger after configure_structlog"


def test_logging_configure_structlog_unknown_level_falls_back_to_info(
    restore_root_logger: None,
) -> None:
    """configure_structlog with an unknown level does not raise."""
    configure_structlog("NOT_A_LEVEL")
    root = stdlib_logging.getLogger()
    assert root.level == stdlib_logging.INFO
