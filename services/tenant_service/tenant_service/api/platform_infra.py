"""Platform infrastructure operations (build.md §18.1: services health, queues, dead letters).

Read endpoints need `platform:infra:read`. Replaying or discarding dead-lettered messages needs
`platform:queues:replay`, step-up MFA and is audited. Only identifiers and metadata are shown:
message bodies are never returned (payloads are ids-only by convention, but this view does not rely on it).
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Annotated, Any, Literal
from urllib.parse import quote

import aio_pika
import httpx
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from nexus_core.authz import AuthContext, require_permission, require_step_up
from nexus_core.celery_app import QUEUES
from nexus_core.errors import NotFound, ServiceUnavailable, Unprocessable
from nexus_core.vault import VaultError, platform_secret_path
from tenant_service.deps import audit, db

router = APIRouter(prefix="/api/v1/platform/infra", tags=["platform-infra"])

Read = Annotated[AuthContext, Depends(require_permission("platform:infra:read"))]
Replay = Annotated[AuthContext, Depends(require_permission("platform:queues:replay"))]
StepUp = Annotated[AuthContext, Depends(require_step_up())]
TASK_DLQS = frozenset(f"{q}.dlq" for q in QUEUES)
SAFE_ID_KEYS_MAX = 12


class ServiceHealth(BaseModel):
    service: str
    status: Literal["ready", "not_ready", "unreachable"]
    checks: dict[str, bool] = {}
    latency_ms: int | None = None


class ServicesOut(BaseModel):
    items: list[ServiceHealth]


class QueueInfo(BaseModel):
    name: str
    kind: Literal["task", "event", "dead_letter", "other"]
    type: str | None = None
    messages: int = 0
    ready: int = 0
    unacked: int = 0
    consumers: int = 0
    publish_rate: float = 0.0
    deliver_rate: float = 0.0


class QueuesOut(BaseModel):
    items: list[QueueInfo]
    dead_letter_total: int


class DeadLetter(BaseModel):
    queue: str
    kind: Literal["task", "event", "unknown"]
    name: str | None = None  # task name or event type
    tenant_id: str | None = None
    correlation_id: str | None = None
    identifiers: dict[str, str] = {}
    death_reason: str | None = None
    death_count: int | None = None
    first_death_queue: str | None = None
    size_bytes: int = 0


class DeadLettersOut(BaseModel):
    queue: str
    items: list[DeadLetter]


class DlqAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queue: str = Field(pattern=r"^[A-Za-z0-9._-]{1,200}\.dlq$")
    count: int = Field(1, ge=1, le=50)
    reason: str = Field(min_length=5, max_length=300)


class DlqActionOut(BaseModel):
    queue: str
    action: Literal["replayed", "discarded"]
    processed: int
    target: str | None = None


async def _ops_credentials(request: Request) -> tuple[str, str]:
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        raise ServiceUnavailable("Broker operations are not configured (Vault unavailable)")
    settings = request.app.state.settings
    try:
        secret = await vault.read(platform_secret_path(settings.env.value, "tenant-service/rabbitmq-ops"))
        return secret["username"].get_secret_value(), secret["password"].get_secret_value()
    except (VaultError, KeyError) as exc:
        raise ServiceUnavailable("Broker operations credentials are not available") from exc


def _kind(name: str) -> Literal["task", "event", "dead_letter", "other"]:
    if name.endswith(".dlq"):
        return "dead_letter"
    if name in QUEUES:
        return "task"
    if name.startswith("events."):
        return "event"
    return "other"


def _replay_target(dlq: str) -> tuple[str, str]:
    """(exchange, routing_key) a dead letter goes back to. Event dead letters return to the one
    consumer queue they came from (default exchange), not to every subscriber of the event."""
    source = dlq.removesuffix(".dlq")
    if dlq in TASK_DLQS:
        return "nexus.tasks", source
    if source.startswith("events."):
        return "", source
    raise Unprocessable("This queue is not a known dead-letter queue")


def _short_ids(data: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if (
                isinstance(value, str)
                and len(value) <= 64
                and key.endswith(("_id", "id"))
                and len(out) < SAFE_ID_KEYS_MAX
            ):
                out[str(key)] = value
    return out


def describe_dead_letter(queue: str, body: bytes, headers: dict[str, Any]) -> DeadLetter:
    """Metadata-only description of a dead-lettered message (never the body itself)."""
    item = DeadLetter(queue=queue, kind="unknown", size_bytes=len(body))
    deaths = headers.get("x-death")
    if isinstance(deaths, list) and deaths and isinstance(deaths[0], dict):
        first = deaths[0]
        item.death_reason = str(first.get("reason")) if first.get("reason") else None
        item.death_count = int(first["count"]) if isinstance(first.get("count"), int) else None
        item.first_death_queue = str(first.get("queue")) if first.get("queue") else None
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return item
    if isinstance(parsed, dict) and "event_type" in parsed:  # event envelope
        item.kind, item.name = "event", str(parsed.get("event_type"))[:128]
        item.tenant_id = str(parsed["tenant_id"]) if parsed.get("tenant_id") else None
        item.correlation_id = str(parsed["correlation_id"]) if parsed.get("correlation_id") else None
        item.identifiers = {"event_id": str(parsed.get("event_id")), **_short_ids(parsed.get("payload"))}
    elif isinstance(parsed, list) and len(parsed) >= 2 and isinstance(parsed[1], dict):  # Celery [args, kwargs, embed]
        kwargs = parsed[1]
        ctx = kwargs.get("ctx") if isinstance(kwargs.get("ctx"), dict) else {}
        item.kind, item.name = "task", str(headers.get("task"))[:128] if headers.get("task") else None
        item.tenant_id = str(ctx["tenant_id"]) if ctx.get("tenant_id") else None
        item.correlation_id = str(ctx["correlation_id"]) if ctx.get("correlation_id") else None
        item.identifiers = _short_ids({k: v for k, v in kwargs.items() if k != "ctx"})
    return item


@router.get("/services", response_model=ServicesOut)
async def services(request: Request, _: Read) -> ServicesOut:
    settings = request.app.state.settings

    async def probe(name: str) -> ServiceHealth:
        url = f"{settings.service_url(name)}/readyz" if name != "web" else "http://web:3000/api/health"
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url)
        except httpx.HTTPError:
            return ServiceHealth(service=name, status="unreachable")
        latency = int((time.perf_counter() - started) * 1000)
        checks: dict[str, bool] = {}
        try:
            raw = resp.json().get("checks", {})
            checks = {str(k): bool(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except ValueError:
            pass
        return ServiceHealth(
            service=name, status="ready" if resp.status_code == 200 else "not_ready", checks=checks, latency_ms=latency
        )

    targets = [*settings.rls_scan_targets, *settings.infra_health_targets]
    return ServicesOut(items=list(await asyncio.gather(*(probe(t) for t in targets))))


async def _management_get(request: Request, path: str) -> Any:
    user, password = await _ops_credentials(request)
    settings = request.app.state.settings
    try:
        async with httpx.AsyncClient(timeout=5.0, auth=(user, password)) as client:
            resp = await client.get(f"{settings.rabbitmq_management_url}{path}")
    except httpx.HTTPError as exc:
        raise ServiceUnavailable("Message broker management API is unreachable") from exc
    if resp.status_code == 404:
        raise NotFound("Queue not found")
    if resp.status_code != 200:
        raise ServiceUnavailable("Message broker management API returned an error")
    return resp.json()


@router.get("/queues", response_model=QueuesOut)
async def queues(request: Request, _: Read) -> QueuesOut:
    vhost = quote(request.app.state.settings.rabbitmq_vhost, safe="")
    rows = await _management_get(
        request,
        f"/queues/{vhost}?columns=name,type,messages,messages_ready," "messages_unacknowledged,consumers,message_stats",
    )
    items = []
    for r in rows:
        stats = r.get("message_stats") or {}
        items.append(
            QueueInfo(
                name=r["name"],
                kind=_kind(r["name"]),
                type=r.get("type"),
                messages=r.get("messages", 0),
                ready=r.get("messages_ready", 0),
                unacked=r.get("messages_unacknowledged", 0),
                consumers=r.get("consumers", 0),
                publish_rate=float((stats.get("publish_details") or {}).get("rate", 0.0)),
                deliver_rate=float((stats.get("deliver_get_details") or {}).get("rate", 0.0)),
            )
        )
    items.sort(key=lambda q: (q.kind != "dead_letter" or q.messages == 0, q.name))
    return QueuesOut(items=items, dead_letter_total=sum(q.messages for q in items if q.kind == "dead_letter"))


async def _amqp(request: Request) -> aio_pika.abc.AbstractRobustConnection:
    user, password = await _ops_credentials(request)
    s = request.app.state.settings
    try:
        return await aio_pika.connect_robust(
            host=s.rabbitmq_amqp_host,
            port=s.rabbitmq_amqp_port,
            login=user,
            password=password,
            virtualhost=s.rabbitmq_vhost,
            timeout=5,
        )
    except Exception as exc:
        raise ServiceUnavailable("Message broker is unreachable") from exc


@router.get("/dead-letters", response_model=DeadLettersOut)
async def dead_letters(
    request: Request,
    _: Read,
    queue: Annotated[str, Query(pattern=r"^[A-Za-z0-9._-]{1,200}\.dlq$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> DeadLettersOut:
    """Peek at dead letters (they stay in the queue). Returns metadata and identifiers only."""
    connection = await _amqp(request)
    items: list[DeadLetter] = []
    async with connection:
        channel = await connection.channel()
        try:
            q = await channel.get_queue(queue, ensure=True)
        except Exception as exc:
            raise NotFound("Queue not found") from exc
        held = []
        for _ in range(limit):
            message = await q.get(no_ack=False, fail=False)
            if message is None:
                break
            held.append(message)
            items.append(describe_dead_letter(queue, message.body, dict(message.headers or {})))
        for message in held:  # put everything back untouched
            await message.nack(requeue=True)
    return DeadLettersOut(queue=queue, items=items)


async def _drain(request: Request, body: DlqAction, *, replay: bool) -> tuple[int, str | None, list[str]]:
    exchange_name, routing_key = _replay_target(body.queue)
    connection = await _amqp(request)
    processed, replay_ids = 0, []
    async with connection:
        channel = await connection.channel(publisher_confirms=True)
        try:
            q = await channel.get_queue(body.queue, ensure=True)
        except Exception as exc:
            raise NotFound("Queue not found") from exc
        exchange = channel.default_exchange if exchange_name == "" else await channel.get_exchange(exchange_name)
        for _ in range(body.count):
            message = await q.get(no_ack=False, fail=False)
            if message is None:
                break
            if replay:
                replay_id = str(uuid.uuid4())
                headers = {
                    k: v
                    for k, v in (message.headers or {}).items()
                    if not k.startswith("x-death")
                    and k
                    not in ("x-first-death-exchange", "x-first-death-queue", "x-first-death-reason", "x-delivery-count")
                }
                headers["x-replay-id"] = replay_id
                # Publish (confirmed) BEFORE acknowledging: a crash in between duplicates the message,
                # which idempotent consumers absorb, rather than losing it.
                await exchange.publish(
                    aio_pika.Message(
                        message.body,
                        headers=headers,
                        content_type=message.content_type,
                        content_encoding=message.content_encoding,
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        correlation_id=message.correlation_id,
                        message_id=message.message_id,
                        type=message.type,
                        reply_to=None,
                    ),
                    routing_key=routing_key,
                )
                replay_ids.append(replay_id)
            await message.ack()
            processed += 1
    target = f"{exchange_name or '(default)'}:{routing_key}" if replay else None
    return processed, target, replay_ids


@router.post("/dead-letters/replay", response_model=DlqActionOut)
async def replay_dead_letters(body: DlqAction, request: Request, ctx: Replay, _: StepUp) -> DlqActionOut:
    processed, target, replay_ids = await _drain(request, body, replay=True)
    async with db(request).global_session(ctx.actor) as s:
        await audit(
            s,
            ctx.actor,
            "platform.dlq.replayed",
            "queue",
            body.queue,
            tenant_id=None,
            count=processed,
            target=target,
            reason=body.reason,
            replay_ids=replay_ids[:50],
        )
    return DlqActionOut(queue=body.queue, action="replayed", processed=processed, target=target)


@router.post("/dead-letters/discard", response_model=DlqActionOut)
async def discard_dead_letters(body: DlqAction, request: Request, ctx: Replay, _: StepUp) -> DlqActionOut:
    processed, _target, _ids = await _drain(request, body, replay=False)
    async with db(request).global_session(ctx.actor) as s:
        await audit(
            s,
            ctx.actor,
            "platform.dlq.discarded",
            "queue",
            body.queue,
            tenant_id=None,
            count=processed,
            reason=body.reason,
        )
    return DlqActionOut(queue=body.queue, action="discarded", processed=processed)
