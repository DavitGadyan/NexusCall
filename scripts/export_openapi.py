#!/usr/bin/env python3
"""Export OpenAPI documents for every service into contracts/openapi/ and event JSON Schemas into
contracts/events/. CI fails if the committed snapshots differ (contract drift)."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ENV", "test")
os.environ.setdefault("OUTBOX_RELAY_ENABLED", "false")
os.environ.setdefault("EVENT_CONSUMERS_ENABLED", "false")

PACKAGES = [
    p.name
    for p in sorted((ROOT / "services").iterdir())
    if (p / p.name / "main.py").exists() and p.name != "fake_providers"
]


def main() -> None:
    out = ROOT / "contracts" / "openapi"
    out.mkdir(parents=True, exist_ok=True)
    for pkg in PACKAGES:
        app = importlib.import_module(f"{pkg}.main").create_app()
        (out / f"{pkg}.json").write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n")
    from nexus_events import EVENT_REGISTRY, EventEnvelope

    ev = ROOT / "contracts" / "events"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "envelope.v1.json").write_text(json.dumps(EventEnvelope.model_json_schema(), indent=2) + "\n")
    for (etype, version), model in sorted(EVENT_REGISTRY.items()):
        (ev / f"{etype}.v{version}.json").write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
    print(f"exported {len(PACKAGES)} OpenAPI documents and {len(EVENT_REGISTRY)} event schemas")


if __name__ == "__main__":
    main()
