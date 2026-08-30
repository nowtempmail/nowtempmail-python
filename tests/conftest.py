"""Shared fixtures.

``src/`` is put on the path here rather than requiring an editable install, so
``python -m pytest`` works in a bare checkout with nothing but pytest present.
That matters more than usual for this package: zero runtime dependencies is a
claim, and a suite that needs an install step to run is a suite that never
proves the claim on a clean machine.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
HELPERS = Path(__file__).resolve().parent

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(HELPERS))

#: What a subprocess needs on PYTHONPATH to import the package from source.
#:
#: `sys.path` above only reaches THIS process. The plugin tests that spawn real
#: subprocesses -- anything under pytest-xdist -- start with a clean interpreter
#: and cannot see it, which surfaces as `Error importing plugin
#: "nowtempmail.pytest_plugin"` rather than as anything to do with what the test
#: is actually checking.
#:
#: It is not set here, because `pytester` deliberately deletes PYTHONPATH from
#: the environment to isolate the sessions it runs. Setting it at import time
#: would be silently undone. The tests that need it set it themselves, after
#: pytester has been constructed.
SUBPROCESS_PYTHONPATH = os.pathsep.join([str(SRC), str(HELPERS)])

from helpers.mock_api import MOCK_KEY, MockApi, mock_api  # noqa: E402


@pytest.fixture
def api() -> Iterator[MockApi]:
    with mock_api() as handle:
        yield handle


@pytest.fixture
def key() -> str:
    return MOCK_KEY
