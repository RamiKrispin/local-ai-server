"""Mint an sk-local- API key, store its Argon2id hash, and print the
plaintext exactly once to stdout."""

import argparse
import secrets
import sqlite3
import sys
from pathlib import Path

# Ensure the project root (parent of scripts/) is on sys.path so that the
# `app` package is importable when the script is run directly via
# `uv run python scripts/generate_api_key.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher

from app.auth import PREFIX_LEN, insert
from app.config import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Mint an sk-local- API key, store its Argon2id hash, "
            "and print the plaintext exactly once."
        )
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Human-readable label for the key (stored in api_keys.name).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Override KEYS_DB_PATH. Defaults to settings.keys_db_path.",
    )
    args = parser.parse_args()
    db_path: Path = args.db or get_settings().keys_db_path

    token = "sk-local-" + secrets.token_urlsafe(32)
    prefix = token[:PREFIX_LEN]
    hash_ = PasswordHasher().hash(token)

    try:
        insert(db_path, prefix=prefix, hash_=hash_, name=args.name)
    except sqlite3.IntegrityError as exc:
        print(
            f"error: prefix collision inserting key: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(token)
    print(
        f"mint complete; prefix={prefix}; "
        "copy the line above into your client",
        file=sys.stderr,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
