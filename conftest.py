"""Repository-wide pytest configuration.

Integration and security tests need Docker (testcontainers). Locally they skip when Docker is not
running. In CI and in release verification set NEXUS_REQUIRE_DOCKER=1: a test that would have been
skipped for that reason then FAILS, so isolation and authorization tests can never pass by not running.
"""

from __future__ import annotations

import os

import pytest

_MARKERS = ("Docker unavailable", "container unavailable")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    outcome = yield
    report = outcome.get_result()
    if os.environ.get("NEXUS_REQUIRE_DOCKER") != "1" or not report.skipped or call.excinfo is None:
        return
    reason = str(call.excinfo.value)
    if any(marker in reason for marker in _MARKERS):
        report.outcome = "failed"
        report.longrepr = f"Docker is required (NEXUS_REQUIRE_DOCKER=1) but was not available: {reason}"
