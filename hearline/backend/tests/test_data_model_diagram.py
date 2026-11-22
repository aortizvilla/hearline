"""The data model diagram in app/database/README.md must match models.py."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("paracelsus", path=str(Path(sys.executable).parent)) is None,
                    reason="paracelsus not installed (pip install -e '.[dev]')")
def test_diagram_is_up_to_date() -> None:
    result = subprocess.run([sys.executable, "-m", "app.database.diagram", "--check"],
                            cwd=BACKEND, capture_output=True, text=True)
    assert result.returncode == 0, "Stale diagram: run `python -m app.database.diagram`"
