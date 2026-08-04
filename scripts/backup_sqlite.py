"""Create a consistent SQLite backup without copying a live database file."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    options = parser.parse_args()
    source = options.source.resolve(strict=True)
    destination = options.destination.resolve(strict=False)
    if destination.exists():
        parser.error("destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with (
        sqlite3.connect(f"file:{source}?mode=ro", uri=True) as original,
        sqlite3.connect(destination) as backup,
    ):
        original.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()
        if result != ("ok",):
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"backup integrity check failed: {result}")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    print(f"backup={destination} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
