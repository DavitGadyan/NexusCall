#!/usr/bin/env python3
"""Create a local .env from .env.example, replacing __generate__ with random dev-only secrets.

Refuses to overwrite an existing .env. Never use these values outside a laptop.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    target = ROOT / ".env"
    if target.exists():
        print(".env already exists; leaving it untouched")
        return 0
    lines = []
    for line in (ROOT / ".env.example").read_text().splitlines():
        if line.endswith("=__generate__"):
            key = line.split("=", 1)[0]
            # Keycloak password policy: >= 12 chars. Alphanumeric avoids shell/URL escaping issues.
            value = secrets.token_hex(24) if key != "NEXUS_SESSION_KEY" else secrets.token_hex(32)
            lines.append(f"{key}={value}")
        else:
            lines.append(line)
    target.write_text("\n".join(lines) + "\n")
    target.chmod(0o600)
    print("wrote .env with random development secrets (mode 600)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
