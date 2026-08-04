from __future__ import annotations

import subprocess
import sys


def test_required_documentation_and_local_links() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/validate_docs.py"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "documentation validation passed" in completed.stdout
