from __future__ import annotations

import subprocess
import sys


def test_direct_dependency_lock_matches_all_project_requirement_groups() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/validate_dependency_lock.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "31 exact direct pins" in completed.stdout
