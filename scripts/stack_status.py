#!/usr/bin/env python3
"""Print a compact status table for the local compose stack (works across compose versions)."""

import json
import subprocess
import sys

out = subprocess.run(
    [
        "docker",
        "compose",
        "--profile",
        "core",
        "--profile",
        "crm",
        "--profile",
        "calls",
        "--profile",
        "email",
        "--profile",
        "platform",
        "--profile",
        "fakes",
        "--profile",
        "obs",
        "ps",
        "-a",
        "--format",
        "json",
    ],
    capture_output=True,
    text=True,
    check=False,
).stdout.strip()
rows = json.loads(out) if out.startswith("[") else [json.loads(x) for x in out.splitlines() if x.strip()]
for r in sorted(rows, key=lambda r: r["Service"]):
    print(f"{r['Service']:24} {r['State']:9} {r.get('Health', '') or '-':10} {r['Status']}")
sys.exit(0)
