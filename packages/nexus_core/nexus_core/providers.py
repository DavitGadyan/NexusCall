"""Provider-adapter selection with a hard production guard against fakes (ADR-0002)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from nexus_core.config import ProviderMode, ServiceSettings

T = TypeVar("T")


class FakeProviderInProduction(RuntimeError):
    pass


def select_adapter(settings: ServiceSettings, *, real: Callable[[], T], fake: Callable[[], T], name: str) -> T:
    if settings.provider_mode is ProviderMode.FAKE:
        if settings.is_production_like:  # double guard (settings validator already rejects this)
            raise FakeProviderInProduction(f"Refusing to use fake {name} provider in {settings.env.value}")
        return fake()
    return real()
