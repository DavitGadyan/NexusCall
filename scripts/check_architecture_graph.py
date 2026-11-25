#!/usr/bin/env python3
"""Validate docs/architecture/architecture.graph.json: JSON Schema + referential integrity."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "architecture"


def main() -> int:
    graph = json.loads((DOC / "architecture.graph.json").read_text())
    jsonschema.validate(graph, json.loads((DOC / "architecture.schema.json").read_text()))
    ids = {n["id"] for n in graph["nodes"]}
    layers = {layer["id"] for layer in graph["layers"]}
    flows = {f["id"] for f in graph["flows"]}
    errors: list[str] = []
    errors += [f"node {n['id']}: unknown layer {n['layer']}" for n in graph["nodes"] if n["layer"] not in layers]
    for e in graph["edges"]:
        errors += [f"edge {e['id']}: unknown {k} {e[k]}" for k in ("from", "to") if e[k] not in ids]
    for f in graph["flows"]:
        for s in f["steps"]:
            errors += [f"flow {f['id']}#{s['n']}: unknown {k} {s[k]}" for k in ("from", "to") if s[k] not in ids]

    def walk(m: dict) -> None:
        errors.extend(f"mindmap {m['id']}: unknown node {r}" for r in m.get("nodeRefs", []) if r not in ids)
        errors.extend(f"mindmap {m['id']}: unknown flow {r}" for r in m.get("flowRefs", []) if r not in flows)
        for c in m.get("children", []):
            walk(c)

    walk(graph["mindmap"])
    if errors:
        print("\n".join(errors))
        return 1
    print(f"architecture graph OK: {len(ids)} nodes, {len(graph['edges'])} edges, {len(flows)} flows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
