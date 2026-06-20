"""Direct CRUD tests for the app.auth key-store helpers.

All tests are unconditional — no live Ollama dependency, no TestClient.
They exercise the SQLite key-store contract directly via the helpers in
app.auth: _init_db, insert, get_row, touch, revoke, KeyRow, PREFIX_LEN.

Test inventory (12 tests):
  - test_keys_db_init_creates_table
  - test_keys_db_init_creates_parent_dirs
  - test_keys_db_insert_roundtrips
  - test_keys_db_get_row_missing_returns_none
  - test_keys_db_insert_duplicate_prefix_raises
  - test_keys_db_touch_updates_last_used_at
  - test_keys_db_touch_missing_prefix_is_silent
  - test_keys_db_revoke_returns_true_on_first_call
  - test_keys_db_revoke_returns_false_on_second_call
  - test_keys_db_revoke_returns_false_on_unknown_prefix
  - test_keys_db_keyrow_is_frozen
  - test_keys_db_prefix_len_is_12
"""
import dataclasses
import sqlite3
import time
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from app.auth import (
    KeyRow,
    PREFIX_LEN,
    _init_db,
    get_row,
    insert,
    revoke,
    touch,
)

_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

_PLAINTEXT = "sk-local-UnitTestKeyDoNotUseInProduction1234"
_PREFIX = _PLAINTEXT[:PREFIX_LEN]
_HASH = _HASHER.hash(_PLAINTEXT)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def test_keys_db_init_creates_table(tmp_path: Path) -> None:
    """_init_db creates the api_keys table; running it twice is a no-op."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
    assert "api_keys" in tables
    # Second call must not raise (CREATE TABLE IF NOT EXISTS).
    _init_db(db_path)


def test_keys_db_init_creates_parent_dirs(tmp_path: Path) -> None:
    """_init_db creates nested parent directories if they don't exist."""
    db_path = tmp_path / "nested" / "deeper" / "keys.db"
    assert not db_path.parent.exists()
    _init_db(db_path)
    assert db_path.exists()


# ---------------------------------------------------------------------------
# Insert / get round-trip
# ---------------------------------------------------------------------------


def test_keys_db_insert_roundtrips(tmp_path: Path) -> None:
    """insert then get_row returns a KeyRow matching the inserted values."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    before = int(time.time())
    insert(db_path, prefix=_PREFIX, hash_=_HASH, name="pytest")
    row = get_row(db_path, _PREFIX)
    assert row is not None
    assert row.prefix == _PREFIX
    assert row.hash == _HASH
    assert row.name == "pytest"
    assert row.created_at >= before
    assert row.last_used_at is None
    assert row.revoked_at is None


def test_keys_db_get_row_missing_returns_none(tmp_path: Path) -> None:
    """get_row on an empty DB returns None without raising."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    result = get_row(db_path, "sk-local-XXXXXXXX")
    assert result is None


def test_keys_db_insert_duplicate_prefix_raises(tmp_path: Path) -> None:
    """A second insert with the same prefix raises sqlite3.IntegrityError."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    insert(db_path, prefix=_PREFIX, hash_=_HASH, name="first")
    with pytest.raises(sqlite3.IntegrityError):
        insert(db_path, prefix=_PREFIX, hash_=_HASH, name="second")


# ---------------------------------------------------------------------------
# touch
# ---------------------------------------------------------------------------


def test_keys_db_touch_updates_last_used_at(tmp_path: Path) -> None:
    """After insert + touch, last_used_at is a positive integer."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    insert(db_path, prefix=_PREFIX, hash_=_HASH, name="pytest")
    before = int(time.time())
    touch(db_path, _PREFIX)
    row = get_row(db_path, _PREFIX)
    assert row is not None
    assert row.last_used_at is not None
    assert row.last_used_at > 0
    assert row.last_used_at >= before


def test_keys_db_touch_missing_prefix_is_silent(tmp_path: Path) -> None:
    """touch on a non-existent prefix does not raise."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    touch(db_path, "nonexistent-prefix")  # must not raise


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_keys_db_revoke_returns_true_on_first_call(tmp_path: Path) -> None:
    """revoke returns True and sets revoked_at on the first call."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    insert(db_path, prefix=_PREFIX, hash_=_HASH, name="pytest")
    result = revoke(db_path, _PREFIX)
    assert result is True
    row = get_row(db_path, _PREFIX)
    assert row is not None
    assert row.revoked_at is not None
    assert row.revoked_at > 0


def test_keys_db_revoke_returns_false_on_second_call(tmp_path: Path) -> None:
    """A second revoke call on an already-revoked key returns False."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    insert(db_path, prefix=_PREFIX, hash_=_HASH, name="pytest")
    revoke(db_path, _PREFIX)
    result = revoke(db_path, _PREFIX)
    assert result is False


def test_keys_db_revoke_returns_false_on_unknown_prefix(
    tmp_path: Path,
) -> None:
    """revoke on a prefix not in the DB returns False without raising."""
    db_path = tmp_path / "keys.db"
    _init_db(db_path)
    result = revoke(db_path, "sk-local-XXXXXXXX")
    assert result is False


# ---------------------------------------------------------------------------
# KeyRow contract
# ---------------------------------------------------------------------------


def test_keys_db_keyrow_is_frozen() -> None:
    """KeyRow is a frozen dataclass; direct attribute assignment raises."""
    row = KeyRow(
        prefix="sk-local-XXXX",
        hash="fakehash",
        name="test",
        created_at=1000000,
        last_used_at=None,
        revoked_at=None,
    )
    # dataclasses.replace works on frozen dataclasses.
    replaced = dataclasses.replace(row, prefix="sk-local-YYYY")
    assert replaced.prefix == "sk-local-YYYY"
    # Direct assignment must raise.
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.prefix = "sk-local-ZZZZ"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Constant lock
# ---------------------------------------------------------------------------


def test_keys_db_prefix_len_is_12() -> None:
    """PREFIX_LEN is exactly 12 — the architectural constant."""
    assert PREFIX_LEN == 12
