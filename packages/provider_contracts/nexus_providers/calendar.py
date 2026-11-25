from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class BusyInterval(BaseModel):
    start: datetime
    end: datetime


class CalendarEvent(BaseModel):
    summary: str
    start: datetime
    end: datetime
    attendee_email: str | None = None
    idempotency_key: str


class CalendarProvider(Protocol):
    name: str

    async def busy(self, access_token: str, calendar_id: str, start: datetime, end: datetime) -> list[BusyInterval]: ...

    async def create_event(self, access_token: str, calendar_id: str, event: CalendarEvent) -> str: ...

    async def cancel_event(self, access_token: str, calendar_id: str, event_id: str) -> None: ...
