"""MicroJS8 version — runtime source of truth.

The string here MUST match the ``version = "..."`` line in
``pyproject.toml``. ``tests/test_version_sync.py`` checks this on every
test run so a drift gets caught before it ships.

Why two places: setuptools' ``attr:`` directive for dynamic versions
fails to import the package during build on some setuptools versions
(seen on Pi OS Bookworm's Python 3.11), so we hardcode the version in
``pyproject.toml`` and keep this module as the single import point at
runtime.
"""

__version__ = "0.1.0"
