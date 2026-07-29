"""Pytest bootstrap for the backend test suite.

``app.core.database`` raises at *import* time when ``DATABASE_URL`` is unset (see
its module top). Several test modules import router code that transitively pulls
in ``app.core.database``, so without this the ValueError is raised during
collection and takes down the entire ``tests/`` directory (19 passing -> 0
collected). conftest.py is imported before any test module, so setting a default
here keeps collection working. Tests that need a real database override it via
their own environment; this default is only a placeholder to satisfy the import.
"""
import os

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost:5432/test",
)
