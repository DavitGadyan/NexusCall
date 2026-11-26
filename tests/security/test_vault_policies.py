"""integration-service may write only the platform secrets its provider catalogue names.

A wildcard would let it overwrite other services' platform secrets (stream-token signing keys,
MCP server credentials). This keeps infra/vault/bootstrap.sh and the catalogue in step.
"""

from __future__ import annotations

import re
from pathlib import Path

from integration_service.providers import catalogue

BOOTSTRAP = Path(__file__).resolve().parents[2] / "infra" / "vault" / "bootstrap.sh"


def _catalogue_pairs() -> set[str]:
    specs = next(
        v
        for v in vars(catalogue).values()
        if isinstance(v, dict) and v and all(hasattr(x, "platform_services") for x in v.values())
    )
    return {f"{svc}/{provider}" for provider, spec in specs.items() for svc in spec.platform_services}


def test_integration_platform_paths_match_the_catalogue() -> None:
    text = BOOTSTRAP.read_text()
    m = re.search(r'^INTEGRATION_PLATFORM_PAIRS="([^"]*)"', text, re.M)
    assert m, "INTEGRATION_PLATFORM_PAIRS missing from bootstrap.sh"
    assert set(m.group(1).split()) == _catalogue_pairs()


def test_no_policy_grants_write_on_all_platform_secrets() -> None:
    for line in BOOTSTRAP.read_text().splitlines():
        if re.search(r"platform/(\+|\*)", line) and re.search(r"create|update|delete", line):
            raise AssertionError(f"wildcard write on platform secrets: {line.strip()}")
