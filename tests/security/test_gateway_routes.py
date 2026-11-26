"""Every public endpoint in the OpenAPI snapshots must be reachable through the gateway.

Regression: /api/v1/platform/overview, /api/v1/platform/feature-flags, /api/v1/support-grants and
/api/v1/platform/ai/tenants/{id}/models existed in their services but had no Traefik route, so the
pages calling them always showed "Not found".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from gen_traefik import ROUTES  # noqa: E402

NOT_PUBLIC = ("/internal/", "/healthz", "/readyz", "/metrics")


def test_every_public_openapi_path_is_routed_to_its_own_service() -> None:
    unrouted = []
    for spec in sorted((ROOT / "contracts" / "openapi").glob("*.json")):
        service = spec.stem.replace("_", "-")
        if service not in ROUTES:
            continue
        for path in json.loads(spec.read_text())["paths"]:
            if path.startswith(NOT_PUBLIC):
                continue
            if not any(path.startswith(prefix) for prefix in ROUTES[service]):
                unrouted.append(f"{service}: {path}")
    assert unrouted == []
