"""Revoke an API key by setting revoked_at = now."""

import argparse
import sys
from pathlib import Path

# Ensure the project root (parent of scripts/) is on sys.path so that the
# `app` package is importable when the script is run directly via
# `uv run python scripts/revoke_api_key.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import revoke  # noqa: E402
from app.config import get_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Revoke an API key by setting revoked_at = now."
    )
    parser.add_argument(
        "--prefix",
        required=True,
        help="The 12-char prefix (sk-local-AbC) printed at mint time.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Override KEYS_DB_PATH.",
    )
    args = parser.parse_args()
    db_path: Path = args.db or get_settings().keys_db_path

    ok = revoke(db_path, args.prefix)
    if ok:
        print(f"revoked: {args.prefix}")
        sys.exit(0)
    print(
        f"unknown or already-revoked prefix: {args.prefix}",
        file=sys.stderr,
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
