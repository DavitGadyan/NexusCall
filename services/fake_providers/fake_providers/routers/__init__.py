"""Auto-discovers provider simulators: every module in this package that defines `router` is mounted.

Add a simulator by creating `routers/<provider>.py` with `router = APIRouter(prefix="/<provider>")`.
"""

from __future__ import annotations

import importlib
import pkgutil

from fastapi import APIRouter

routers: list[APIRouter] = []
for _module in sorted(pkgutil.iter_modules(__path__), key=lambda m: m.name):
    _router = getattr(importlib.import_module(f"{__name__}.{_module.name}"), "router", None)
    if isinstance(_router, APIRouter):
        routers.append(_router)
