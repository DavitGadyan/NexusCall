"""Repository-wide pytest configuration.

Integration and security tests need Docker (testcontainers). Locally they skip when Docker is not
running. In CI and in release verification set NEXUS_REQUIRE_DOCKER=1: a test that would have been
skipped for that reason then FAILS, so isolation and authorization tests can never pass by not running.
"""

from __future__ import annotations

import ipaddress
import os
import socket

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


# --------------------------------------------------------------------------------------------- network guard
# Tests must never reach the public internet (a test once sent requests to real AWS SES because an HTTP
# client was not intercepted). DNS and TCP are allowed only to loopback, private ranges and Docker.
# Set NEXUS_ALLOW_NETWORK=1 only for deliberate live-provider contract tests.
_ALLOWED_NAMES = {"localhost", "host.docker.internal", "docker", "testcontainers"}


def _is_local_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value.split("%")[0])
    except ValueError:
        return False
    # Cloud metadata endpoints are never allowed. Note: Python counts 169.254.0.0/16 as "private".
    if str(ip) in ("169.254.169.254", "fd00:ec2::254"):
        return False
    return ip.is_loopback or ip.is_private or ip.is_link_local


class PublicNetworkBlocked(OSError):
    pass


@pytest.fixture(autouse=True, scope="session")
def _block_public_network():  # type: ignore[no-untyped-def]
    if os.environ.get("NEXUS_ALLOW_NETWORK") == "1":
        yield
        return
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect

    def guarded_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        name = host.decode() if isinstance(host, bytes) else str(host or "")
        if name and name.lower() not in _ALLOWED_NAMES and not _is_local_ip(name):
            raise PublicNetworkBlocked(f"tests may not resolve public host {name!r} (set NEXUS_ALLOW_NETWORK=1)")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_connect(self, address):  # type: ignore[no-untyped-def]
        inet = self.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple)
        target = str(address[0]) if inet else ""
        if inet and target.lower() not in _ALLOWED_NAMES and not _is_local_ip(target):
            raise PublicNetworkBlocked(f"tests may not connect to {address[0]!r} (set NEXUS_ALLOW_NETWORK=1)")
        return real_connect(self, address)

    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]
    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo  # type: ignore[assignment]
        socket.socket.connect = real_connect  # type: ignore[method-assign]
