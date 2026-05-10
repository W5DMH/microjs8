"""Catch drift between version.py and pyproject.toml.

Two places have to agree on the version string. This test ensures we
notice if someone bumps one but forgets the other, before that drift
ships in an image.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from microjs8 import __version__


def test_version_matches_pyproject():
    project_root = Path(__file__).parent.parent
    pyproject = project_root / "pyproject.toml"
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    declared = data["project"]["version"]
    assert declared == __version__, (
        f"version mismatch: pyproject.toml says {declared!r}, "
        f"src/microjs8/version.py says {__version__!r}; "
        f"update both"
    )


def test_version_format():
    """Sanity check that the version is a non-empty string of dots and digits."""
    assert isinstance(__version__, str)
    assert re.match(r"^\d+\.\d+\.\d+(\.\w+)?$", __version__), (
        f"version {__version__!r} does not look like SemVer"
    )
