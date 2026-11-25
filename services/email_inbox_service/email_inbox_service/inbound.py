"""Inbound email: verified SES/SNS notification -> provider_events row -> `emails.inbound_process` task.

Trust rules (build.md §4.2, BUILD_CONVENTIONS §3 Webhooks):
* The SNS signature, certificate host, topic allowlist and timestamp are verified before anything is read.
* The tenant and mailbox come ONLY from the SES envelope recipients (`receipt.recipients`, signed by SNS)
  through `mailbox_directory`. `To`/`Cc`/`X-*` headers written by the sender never select a tenant.
* Dedupe happens twice: on the SNS MessageId (unique provider_events row) and on the RFC `Message-ID`
  (unique per tenant in `messages`).
* Email content is untrusted. It is stored sanitized and only ever reaches a model as untrusted data.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from email_inbox_service.deps import Deps, audit, event, job_context, record_action
from email_inbox_service.mime import (
    BLOCKED_EXTENSIONS,
    ParsedAttachment,
    address_hash,
    auto_reply_flags,
    extension,
    normalize_address,
    parse_email,
)
from email_inbox_service.scanner import ScannerUnavailable, scan_bytes
from email_inbox_service.settings import DRAFT_QUEUE, DRAFT_TASK, INBOUND_QUEUE, INBOUND_TASK
from email_inbox_service.tables import (
    attachments,
    drafts,
    mailbox_directory,
    mailboxes,
    messages,
    provider_events,
    threads,
)
from nexus_core.celery_app import JobContext, TransientFailure
from nexus_core.clients import UpstreamError, UpstreamUnavailable
from nexus_core.config import Environment, ProviderMode
from nexus_core.context import SYSTEM_ACTOR, Actor
from nexus_core.storage import tenant_key
from nexus_core.webhooks import SnsVerifier

log = logging.getLogger(__name__)
INBOUND_ACTOR = Actor("system", "email-inbound")
_OBJECT_KEY = re.compile(r"^[A-Za-z0-9!_.*'()/=-]{1,512}$")
_VERDICT = re.compile(r"^[A-Z_]{1,32}$")
_SUBSCRIBE_HOST = re.compile(r"^https://sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?/")
_MAGIC = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/gif": (b"GIF87a", b"GIF89a"),
}


class InvalidNotification(ValueError):
    pass


# ------------------------------------------------------------------------------------------- webhook side
def build_verifier(settings: Any) -> SnsVerifier:
    if settings.provider_mode is ProviderMode.FAKE:
        # Development/test simulator only (settings refuse fake mode in staging/production).
        return SnsVerifier(
            cert_host_pattern=r"^fake-providers$",
            allowed_topic_arns=frozenset({settings.fake_ses_inbound_topic_arn}),
            allow_http=True,
            verify_tls=False,
        )
    return SnsVerifier(allowed_topic_arns=frozenset(settings.ses_inbound_topic_arns))


def parse_envelope(raw: bytes) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidNotification("body is not JSON") from exc
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise InvalidNotification("unexpected SNS envelope shape")
    return data


async def confirm_subscription(settings: Any, envelope: dict[str, str]) -> str:
    """Only after signature + topic verification; only to the real SNS host over HTTPS; no redirects."""
    if settings.provider_mode is ProviderMode.FAKE:
        return "subscription_acknowledged"
    url = envelope.get("SubscribeURL", "")
    if not _SUBSCRIBE_HOST.match(url):
        raise InvalidNotification("SubscribeURL host is not an SNS endpoint")
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise InvalidNotification("subscription confirmation failed")
    return "subscription_confirmed"


def _verdicts(receipt: dict[str, Any]) -> dict[str, str]:
    out = {}
    for key in ("spamVerdict", "virusVerdict", "spfVerdict", "dkimVerdict", "dmarcVerdict"):
        status = (receipt.get(key) or {}).get("status")
        if isinstance(status, str) and _VERDICT.match(status):
            out[key.removesuffix("Verdict")] = status
    return out


def parse_receipt(settings: Any, message: str) -> tuple[list[str], str, str, str | None, dict[str, str]]:
    """-> (envelope recipients, bucket, object key, SES message id, verdicts). Raises InvalidNotification."""
    try:
        body = json.loads(message)
    except ValueError as exc:
        raise InvalidNotification("SNS Message is not JSON") from exc
    if not isinstance(body, dict) or body.get("notificationType") != "Received":
        raise InvalidNotification("not an SES receipt notification")
    receipt = body.get("receipt") or {}
    action = receipt.get("action") or {}
    if action.get("type") != "S3":
        raise InvalidNotification("receipt action must be S3")
    bucket, key = str(action.get("bucketName", "")), str(action.get("objectKey", ""))
    if bucket != settings.ses_inbound_bucket:
        raise InvalidNotification("unexpected bucket")
    if not (_OBJECT_KEY.match(key) and key.startswith(settings.ses_inbound_key_prefix) and ".." not in key):
        raise InvalidNotification("unexpected object key")
    raw_recipients = receipt.get("recipients")
    if not isinstance(raw_recipients, list) or not raw_recipients:
        raise InvalidNotification("no envelope recipients")
    recipients = []
    for value in raw_recipients[:50]:
        addr = normalize_address(str(value))
        if addr and addr not in recipients:
            recipients.append(addr)
    ses_id = (body.get("mail") or {}).get("messageId")
    ses_message_id = str(ses_id)[:128] if isinstance(ses_id, str) else None
    return recipients, bucket, key, ses_message_id, _verdicts(receipt)


async def record_notification(deps: Deps, envelope: dict[str, str]) -> dict[str, Any]:
    """Persist one queued provider_events row per known recipient mailbox; enqueue after commit."""
    recipients, bucket, key, ses_message_id, verdicts = parse_receipt(deps.settings, envelope.get("Message", ""))
    sns_id = envelope.get("MessageId", "")[:128]
    if not sns_id:
        raise InvalidNotification("missing MessageId")
    created: list[tuple[uuid.UUID, uuid.UUID]] = []
    known = 0
    async with deps.db.global_session(INBOUND_ACTOR) as s:
        rows = (
            await s.execute(
                select(mailbox_directory.c.tenant_id, mailbox_directory.c.mailbox_id).where(
                    mailbox_directory.c.address_hash.in_([address_hash(a) for a in recipients]),
                    mailbox_directory.c.status == "active",
                )
            )
        ).all()
        for row in rows:
            known += 1
            event_row = (
                await s.execute(
                    insert(provider_events)
                    .values(
                        provider="ses",
                        event_id=sns_id,
                        tenant_id=row.tenant_id,
                        mailbox_id=row.mailbox_id,
                        bucket=bucket,
                        object_key=key,
                        ses_message_id=ses_message_id,
                        verdicts=verdicts,
                    )
                    .on_conflict_do_nothing(constraint="uq_provider_events_event")
                    .returning(provider_events.c.id)
                )
            ).scalar_one_or_none()
            if event_row is not None:
                created.append((event_row, row.tenant_id))
    for event_id, tenant_id in created:
        await deps.enqueue(INBOUND_TASK, INBOUND_QUEUE, job_context(tenant_id, INBOUND_ACTOR), event_id=str(event_id))
    if known == 0:
        log.info("inbound_unknown_recipient", extra={"sns_message_id": sns_id})
        return {"status": "ignored"}
    return {"status": "accepted" if created else "duplicate", "queued": len(created)}


# ------------------------------------------------------------------------------------------- processing
async def _set_event(deps: Deps, event_id: uuid.UUID, status: str, *, error: str | None = None, **values: Any) -> None:
    async with deps.db.global_session(INBOUND_ACTOR) as s:
        await s.execute(
            update(provider_events)
            .where(provider_events.c.id == event_id)
            .values(status=status, last_error=error, updated_at=func.now(), **values)
        )


async def _lookup_campaign(deps: Deps, tenant_id: uuid.UUID, ids: list[str]) -> dict[str, Any] | None:
    """Replies to campaign mail: email-service knows the Message-IDs it sent."""
    client = deps.client("email-service")
    for rfc_id in ids[-5:]:
        try:
            found = await client.get_json(
                "/internal/v1/messages/lookup",
                params={"rfc_message_id": rfc_id},
                tenant_id=tenant_id,
                actor=INBOUND_ACTOR,
            )
        except UpstreamError:
            continue
        except UpstreamUnavailable:
            return None
        if isinstance(found, dict) and found.get("message_id"):
            return found
    return None


async def _lookup_contact(deps: Deps, tenant_id: uuid.UUID, address: str | None) -> uuid.UUID | None:
    if address is None:
        return None
    try:
        found = await deps.client("crm-service").get_json(
            "/internal/v1/contacts/lookup", params={"email": address}, tenant_id=tenant_id, actor=INBOUND_ACTOR
        )
        return uuid.UUID(str(found["contact_id"])) if found and found.get("contact_id") else None
    except (UpstreamError, UpstreamUnavailable, KeyError, ValueError):
        return None


async def sender_suppressed(deps: Deps, tenant_id: uuid.UUID, address: str, actor: Actor) -> bool | None:
    """True / False from crm-service; None when crm could not answer (callers fail closed later)."""
    try:
        res = await deps.client("crm-service").get_json(
            "/internal/v1/suppressions/check",
            params={"channel": "email", "value": address},
            tenant_id=tenant_id,
            actor=actor,
        )
    except UpstreamUnavailable:
        return None
    except UpstreamError as exc:
        if exc.upstream_status == 422:  # crm says the address is invalid: never reply to it
            return True
        return None
    return bool(res.get("suppressed", True))


def _attachment_policy(settings: Any, att: ParsedAttachment, index: int, total_bytes: int) -> str | None:
    if index >= settings.max_attachments:
        return "too_many_attachments"
    if len(att.data) > settings.max_attachment_bytes or total_bytes > settings.max_raw_mime_bytes:
        return "too_large"
    if extension(att.filename) in BLOCKED_EXTENSIONS or att.content_type not in settings.allowed_attachment_types:
        return "type_not_allowed"
    magic = _MAGIC.get(att.content_type)
    if magic and not att.data.startswith(magic):
        return "type_mismatch"
    return None


async def _store_attachments(
    deps: Deps, tenant_id: uuid.UUID, message_id: uuid.UUID, parts: list[ParsedAttachment], ses_virus_fail: bool
) -> list[dict[str, Any]]:
    settings, store = deps.settings, deps.store
    rows: list[dict[str, Any]] = []
    total = 0
    dev = settings.env in (Environment.DEVELOPMENT, Environment.TEST)
    for index, att in enumerate(parts):
        total += len(att.data)
        att_id = uuid.uuid4()
        row: dict[str, Any] = {
            "id": att_id,
            "tenant_id": tenant_id,
            "message_id": message_id,
            "filename": att.filename,
            "content_type": att.content_type,
            "size_bytes": len(att.data),
            "sha256": att.sha256,
            "object_key": None,
            "scan_signature": None,
            "quarantined": False,
            "rejection_reason": None,
        }
        rejection = _attachment_policy(settings, att, index, total)
        if rejection is not None:
            rows.append({**row, "scan_status": "rejected", "rejection_reason": rejection})
            continue
        try:
            result = await scan_bytes(
                settings.clamav_host, settings.clamav_port, att.data, timeout_seconds=settings.clamav_timeout_seconds
            )
            status, signature = ("infected", result.signature) if result.infected else ("clean", None)
        except ScannerUnavailable:
            status, signature = ("skipped" if dev else "error"), None
        if ses_virus_fail and status != "infected":
            status, signature = "infected", "ses_virus_verdict"
        quarantined = status == "infected"
        key = tenant_key(tenant_id, "quarantine" if quarantined else "files", str(att_id))
        if store is None:
            raise TransientFailure("object store not configured")
        await store.put("attachments", key, att.data, content_type="application/octet-stream")
        rows.append(
            {**row, "scan_status": status, "scan_signature": signature, "quarantined": quarantined, "object_key": key}
        )
    return rows


async def process_inbound(deps: Deps, ctx: JobContext, event_id: uuid.UUID) -> str:
    """`emails.inbound_process`. Idempotent: a duplicate delivery finds the row no longer `queued`."""
    settings, store = deps.settings, deps.store
    async with deps.db.global_session(INBOUND_ACTOR) as s:
        claimed = (
            (
                await s.execute(
                    update(provider_events)
                    .where(provider_events.c.id == event_id, provider_events.c.status == "queued")
                    .values(status="processing", attempts=provider_events.c.attempts + 1, updated_at=func.now())
                    .returning(provider_events)
                )
            )
            .mappings()
            .first()
        )
    if claimed is None:
        return "skipped"
    tenant_id: uuid.UUID = claimed["tenant_id"]
    if tenant_id != ctx.tenant_id:  # the job context must agree with the trusted row
        await _set_event(deps, event_id, "failed", error="tenant_mismatch")
        return "failed"
    if store is None:
        await _set_event(deps, event_id, "queued", error="store_unavailable")
        raise TransientFailure("object store not configured")
    try:
        raw = await store.get(claimed["bucket"], claimed["object_key"], max_bytes=settings.max_raw_mime_bytes)
    except ValueError:
        await _set_event(deps, event_id, "failed", error="too_large")
        return "failed"
    except KeyError:
        await _set_event(deps, event_id, "failed", error="raw_missing")
        return "failed"
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code") if hasattr(exc, "response") else None
        if code in ("NoSuchKey", "404", "NotFound"):
            await _set_event(deps, event_id, "failed", error="raw_missing")
            return "failed"
        await _set_event(deps, event_id, "queued", error="store_error")
        raise TransientFailure("object store error") from exc
    try:
        return await _ingest(deps, claimed, raw)
    except TransientFailure:
        await _set_event(deps, event_id, "queued", error="transient")
        raise


async def _ingest(deps: Deps, ev: Any, raw: bytes) -> str:  # noqa: PLR0912, PLR0915 - one linear pipeline
    settings = deps.settings
    tenant_id: uuid.UUID = ev["tenant_id"]
    mailbox_id: uuid.UUID = ev["mailbox_id"]
    actor = INBOUND_ACTOR
    async with deps.db.tenant_session(tenant_id, actor) as s:
        mailbox = (await s.execute(select(mailboxes).where(mailboxes.c.id == mailbox_id))).mappings().first()
    if mailbox is None or mailbox["status"] != "active":
        await _set_event(deps, ev["id"], "failed", error="mailbox_inactive")
        return "failed"
    msg, parsed = parse_email(raw, max_text_chars=settings.max_text_chars)
    flags = auto_reply_flags(msg, parsed.from_address, mailbox["address"])
    verdicts = ev["verdicts"] or {}
    if verdicts.get("spam") == "FAIL":
        flags.append("ses_spam")
    if verdicts.get("virus") == "FAIL":
        flags.append("ses_virus")
    rfc_id = parsed.message_id or f"<sns-{ev['event_id']}@inbound.nexus.invalid>"
    refs = [r for r in [*parsed.references, parsed.in_reply_to] if r]

    async with deps.db.tenant_session(tenant_id, actor) as s:
        if (await s.execute(select(messages.c.id).where(messages.c.rfc_message_id == rfc_id))).first():
            duplicate = True
        else:
            duplicate = False
            known = (
                (
                    await s.execute(
                        select(messages.c.thread_id)
                        .where(messages.c.rfc_message_id.in_(refs), messages.c.mailbox_id == mailbox_id)
                        .order_by(messages.c.received_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if refs
                else None
            )
    if duplicate:
        await _set_event(deps, ev["id"], "duplicate")
        return "duplicate"

    campaign = None if known or not refs else await _lookup_campaign(deps, tenant_id, refs)
    contact_id = None
    if campaign and campaign.get("contact_id"):
        try:
            contact_id = uuid.UUID(str(campaign["contact_id"]))
        except ValueError:
            contact_id = None
    if contact_id is None:
        contact_id = await _lookup_contact(deps, tenant_id, parsed.from_address)
    campaign_id = None
    if campaign and campaign.get("campaign_id"):
        try:
            campaign_id = uuid.UUID(str(campaign["campaign_id"]))
        except ValueError:
            campaign_id = None

    wants_draft = bool(mailbox["auto_draft"]) and not flags and parsed.from_address is not None
    suppressed: bool | None = False
    if wants_draft:
        suppressed = await sender_suppressed(deps, tenant_id, parsed.from_address or "", actor)
        if suppressed:
            flags.append("sender_suppressed")
    create_draft = wants_draft and not suppressed  # None (crm unavailable): the draft task re-checks

    message_id = uuid.uuid4()
    att_rows = await _store_attachments(deps, tenant_id, message_id, parsed.attachments, "ses_virus" in flags)
    raw_key = tenant_key(tenant_id, "raw", f"{message_id}.eml")
    assert deps.store is not None
    await deps.store.put("raw-mail", raw_key, raw, content_type="message/rfc822")
    now = datetime.now(UTC)
    draft_id: uuid.UUID | None = None
    async with deps.db.tenant_session(tenant_id, actor) as s:
        thread_id = known
        if thread_id is None:
            thread_id = (
                await s.execute(
                    insert(threads)
                    .values(
                        tenant_id=tenant_id,
                        mailbox_id=mailbox_id,
                        subject=parsed.subject,
                        contact_id=contact_id,
                        campaign_id=campaign_id,
                        last_message_at=now,
                    )
                    .returning(threads.c.id)
                )
            ).scalar_one()
        inserted = (
            await s.execute(
                insert(messages)
                .values(
                    id=message_id,
                    tenant_id=tenant_id,
                    thread_id=thread_id,
                    mailbox_id=mailbox_id,
                    direction="inbound",
                    rfc_message_id=rfc_id,
                    in_reply_to=parsed.in_reply_to,
                    rfc_references=parsed.references,
                    from_address=parsed.from_address or "unknown@invalid",
                    from_name=parsed.from_name,
                    to_addresses=parsed.to_addresses,
                    cc_addresses=parsed.cc_addresses,
                    subject=parsed.subject,
                    text_body=parsed.text,
                    html_sanitized=parsed.html,
                    raw_object_key=raw_key,
                    sns_message_id=ev["event_id"],
                    size_bytes=len(raw),
                    auto_flags=sorted(set(flags)),
                    contact_id=contact_id,
                    campaign_id=campaign_id,
                    received_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_messages_rfc_message_id")
                .returning(messages.c.id)
            )
        ).scalar_one_or_none()
        if inserted is None:  # lost a race with a concurrent duplicate: roll back everything
            raise _Duplicate
        if att_rows:
            await s.execute(insert(attachments), att_rows)
        thread_values: dict[str, Any] = {
            "message_count": threads.c.message_count + 1,
            "last_message_at": now,
            "updated_at": func.now(),
            "version": threads.c.version + 1,
        }
        if create_draft:
            thread_values["status"] = "awaiting_review"
        else:
            thread_values["status"] = "open"
        await s.execute(update(threads).where(threads.c.id == thread_id).values(**thread_values))
        await event(
            s,
            "email.inbound.received",
            tenant_id=tenant_id,
            actor=actor,
            payload={"message_id": str(message_id), "thread_id": str(thread_id), "mailbox_id": str(mailbox_id)},
        )
        if create_draft:
            draft_id = (
                await s.execute(
                    insert(drafts)
                    .values(tenant_id=tenant_id, thread_id=thread_id, message_id=message_id, mailbox_id=mailbox_id)
                    .on_conflict_do_nothing(constraint="uq_drafts_message")
                    .returning(drafts.c.id)
                )
            ).scalar_one_or_none()
            if draft_id is not None:
                await record_action(s, tenant_id=tenant_id, draft_id=draft_id, action="requested", actor=actor)
                await event(
                    s,
                    "email.reply_draft.requested",
                    tenant_id=tenant_id,
                    actor=actor,
                    payload={"message_id": str(message_id), "thread_id": str(thread_id), "mailbox_id": str(mailbox_id)},
                )
        infected = sum(1 for a in att_rows if a["scan_status"] == "infected")
        if infected:
            await audit(
                s, actor, "email.attachment.quarantined", "message", message_id, tenant_id=tenant_id, count=infected
            )
    await _set_event(deps, ev["id"], "processed", message_id=message_id)
    try:
        await deps.store.delete(ev["bucket"], ev["object_key"])
    except Exception:  # noqa: BLE001 - the retention job removes leftovers
        log.warning("raw_original_not_deleted", extra={"message_id": str(message_id)})
    if draft_id is not None:
        await deps.enqueue(DRAFT_TASK, DRAFT_QUEUE, job_context(tenant_id, SYSTEM_ACTOR), draft_id=str(draft_id))
    return "processed"


class _Duplicate(Exception):
    pass


async def process_inbound_safe(deps: Deps, ctx: JobContext, event_id: uuid.UUID) -> str:
    """Wrapper that turns a lost Message-ID race into a recorded duplicate."""
    try:
        return await process_inbound(deps, ctx, event_id)
    except _Duplicate:
        await _set_event(deps, event_id, "duplicate")
        return "duplicate"
