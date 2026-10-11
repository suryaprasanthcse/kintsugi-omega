"""Shared test configuration.

Tests marked ``contract`` exercise the real Entire, entire-graph and Claude Code binaries. They run
only with ``KINTSUGI_CONTRACT=1`` (they need local tooling and, for headless sessions, a
logged-in ``claude`` CLI); CI runs every other test against the golden fixtures they pin.
"""

from __future__ import annotations

import os

import pytest
from hypothesis import settings

settings.register_profile("deterministic", derandomize=True, deadline=None)
settings.load_profile("deterministic")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("KINTSUGI_CONTRACT") == "1":
        return
    skip = pytest.mark.skip(reason="contract test: set KINTSUGI_CONTRACT=1 (needs local tooling)")
    for item in items:
        if "contract" in item.keywords:
            item.add_marker(skip)
