"""Remove only documented, repository-local generated caches and demo output."""

from __future__ import annotations

import shutil
from pathlib import Path


def main() -> int:
    root = Path.cwd().resolve()
    targets = (root / ".coverage", root / "coverage.xml", root / ".demo")
    for target in targets:
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
    for name in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        for target in root.rglob(name):
            if target.is_dir() and root in target.parents:
                shutil.rmtree(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
