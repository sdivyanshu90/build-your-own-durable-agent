"""Verify exact project requirements and the direct-dependency constraint lock agree."""

from __future__ import annotations

import sys
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised by the managed Python 3.10 host
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_EXTRA_CONSTRAINTS = frozenset({"psycopg-binary", "pydantic-core"})


def exact_pin(requirement: Requirement) -> str:
    """Return one exact version or fail with an actionable lock error."""
    specifiers = tuple(requirement.specifier)
    if (
        len(specifiers) != 1
        or specifiers[0].operator != "=="
        or specifiers[0].version.endswith(".*")
    ):
        raise ValueError(f"requirement is not exactly pinned: {requirement}")
    return specifiers[0].version


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared_text = list(project["dependencies"])
    for values in project.get("optional-dependencies", {}).values():
        declared_text.extend(values)
    declared: dict[str, str] = {}
    for text in declared_text:
        requirement = Requirement(text)
        name = canonicalize_name(requirement.name)
        version = exact_pin(requirement)
        previous = declared.setdefault(name, version)
        if previous != version:
            raise ValueError(f"project declares conflicting versions for {name}")

    locked: dict[str, str] = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        version = exact_pin(requirement)
        if name in locked:
            raise ValueError(f"lock contains duplicate constraint: {name}")
        locked[name] = version

    missing = sorted(set(declared) - set(locked))
    unexpected = sorted(set(locked) - set(declared) - ALLOWED_EXTRA_CONSTRAINTS)
    mismatched = sorted(
        name for name in declared.keys() & locked.keys() if declared[name] != locked[name]
    )
    if missing or unexpected or mismatched:
        print(
            f"dependency lock mismatch: missing={missing}, unexpected={unexpected}, "
            f"versions={mismatched}",
            file=sys.stderr,
        )
        return 1
    print(f"dependency lock validation passed: {len(declared)} exact direct pins")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
