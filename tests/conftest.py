"""Shared fixtures.

Real SQLite, file-backed (not `:memory:`) so the setup matches how the
library is used in production and how phase-2 multi-connection tests
will exercise the worker.
"""

from __future__ import annotations

import pytest

from recorded import Recorder
from recorded import _recorder as _recorder_mod
from recorded import _registry as _registry_mod


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "jobs.db")


@pytest.fixture
def recorder(db_path):
    r = Recorder(path=db_path)
    try:
        yield r
    finally:
        r.shutdown()


@pytest.fixture
def default_recorder(recorder):
    """Install `recorder` as the module-level default for the test."""
    _recorder_mod._set_default(recorder)
    try:
        yield recorder
    finally:
        _recorder_mod._set_default(None)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the kind registry between tests so adapters don't leak."""
    yield
    _registry_mod._reset()
