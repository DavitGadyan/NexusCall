"""LOCAL DEVELOPMENT demo data: per demo tenant an active voice agent (`demo_id("agent", 1|101)`), an active
email agent (`2|102`) and a small knowledge source (hours, services, pricing FAQ).

Idempotent (fixed ids, ON CONFLICT DO NOTHING). Runs as the restricted `agent_app` role, so RLS applies.
Chunks are inserted WITHOUT embeddings plus a queued re-embed job: the running service's sweeper embeds them
through the AI Gateway, so the seed never needs a model provider and never fabricates vectors."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert

from ai_agent_service.domain import chunk_text, sha256_hex
from ai_agent_service.tables import (
    agent_jobs,
    agents,
    knowledge_chunks,
    knowledge_documents,
    knowledge_sources,
    prompt_versions,
)
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, GLOBEX, SeedTenant, demo_id

SEED_ACTOR = Actor("system", "dev-seed")

FAQ = {
    ACME.slug: (
        "Opening hours\n\nAcme Home Services is open Monday to Friday from 8am to 6pm and Saturday from 9am to "
        "1pm. We are closed on Sundays and public holidays.\n\n"
        "Services\n\nWe offer plumbing repairs, water heater installation, drain cleaning and annual boiler "
        "servicing in the metro area.\n\n"
        "Pricing\n\nA standard call-out visit costs $89 and includes the first 30 minutes of labour. Additional "
        "labour is $45 per 30 minutes. Annual boiler servicing costs $129. Quotes for installations are free."
    ),
    GLOBEX.slug: (
        "Opening hours\n\nGlobex Dental is open Monday to Thursday from 9am to 5pm and Friday from 9am to 3pm.\n\n"
        "Services\n\nWe provide check-ups, cleanings, fillings, whitening and emergency appointments.\n\n"
        "Pricing\n\nA check-up with cleaning costs $120. Whitening costs $350. We accept most major dental "
        "insurance plans; please bring your insurance card to your first visit."
    ),
}
VOICE_PROMPT = (
    "You are the friendly phone assistant for {name}. Help callers with questions about opening hours, "
    "services and prices using the knowledge search tool, and offer to find an appointment slot. Keep answers "
    "short. If the caller asks for a human, wants to complain or raises anything legal, transfer the call."
)
EMAIL_PROMPT = (
    "You draft polite, concise email replies for {name}. Answer questions using the knowledge excerpts you are "
    "given, cite them, and hand anything sensitive to a colleague. Sign off as 'The {name} team'."
)
TENANTS: tuple[tuple[SeedTenant, int], ...] = ((ACME, 0), (GLOBEX, 100))


async def seed(db: Database) -> None:
    for tenant, offset in TENANTS:
        source_id = demo_id("knowledge_source", offset + 1)
        document_id = demo_id("knowledge_document", offset + 1)
        text = FAQ[tenant.slug]
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            await s.execute(
                insert(knowledge_sources)
                .values(
                    id=source_id,
                    tenant_id=tenant.id,
                    name="Business FAQ",
                    description="Opening hours, services and pricing (demo data)",
                    origin="internal",
                    classification="internal",
                    created_by=str(SEED_ACTOR),
                )
                .on_conflict_do_nothing()
            )
            inserted = (
                await s.execute(
                    insert(knowledge_documents)
                    .values(
                        id=document_id,
                        tenant_id=tenant.id,
                        source_id=source_id,
                        filename="faq.md",
                        content_type="text/markdown",
                        size_bytes=len(text.encode()),
                        sha256=sha256_hex(text),
                        object_key=None,
                        uploaded_by=str(SEED_ACTOR),
                        status="indexed",
                        review_state="not_required",
                        scan_status="clean",
                        char_count=len(text),
                    )
                    .on_conflict_do_nothing()
                    .returning(knowledge_documents.c.id)
                )
            ).first()
            if inserted is not None:
                chunks = chunk_text(text, size=400, overlap=0)
                for ch in chunks:
                    await s.execute(
                        insert(knowledge_chunks).values(
                            tenant_id=tenant.id,
                            source_id=source_id,
                            document_id=document_id,
                            ordinal=ch.ordinal,
                            text=ch.text,
                            content_sha256=ch.sha256,
                        )
                    )
                await s.execute(
                    knowledge_documents.update()
                    .where(knowledge_documents.c.id == document_id)
                    .values(chunk_count=len(chunks))
                )
                await s.execute(
                    insert(agent_jobs)
                    .values(
                        job_id=demo_id("reembed_job", offset + 1),
                        tenant_id=tenant.id,
                        kind="reembed",
                        updated_at=datetime(2026, 1, 1, tzinfo=UTC),  # due on the first sweep
                    )
                    .on_conflict_do_nothing()
                )
            for n, kind, prompt, tools in (
                (
                    offset + 1,
                    "voice",
                    VOICE_PROMPT,
                    ["booking.create_tentative", "booking.find_slots", "knowledge.search"],
                ),
                (offset + 2, "email", EMAIL_PROMPT, []),
            ):
                agent_id = demo_id("agent", n)
                created = (
                    await s.execute(
                        insert(agents)
                        .values(
                            id=agent_id,
                            tenant_id=tenant.id,
                            name=f"{tenant.name} {kind} agent",
                            kind=kind,
                            status="draft",
                            latest_version=1,
                            created_by=str(SEED_ACTOR),
                        )
                        .on_conflict_do_nothing()
                        .returning(agents.c.id)
                    )
                ).first()
                if created is None:
                    continue
                await s.execute(
                    insert(prompt_versions).values(
                        tenant_id=tenant.id,
                        agent_id=agent_id,
                        version=1,
                        system_prompt=prompt.format(name=tenant.name),
                        greeting=f"Hi, thanks for calling {tenant.name}." if kind == "voice" else "",
                        disclosure_text="You are speaking with an AI assistant. This call may be recorded."
                        if kind == "voice"
                        else "",
                        model="claude-haiku-5-5" if kind == "voice" else "claude-sonnet-5-5",
                        voice_id="demo-voice-1" if kind == "voice" else None,
                        tool_ids=tools,
                        knowledge_source_ids=[source_id],
                        escalation_rules=[{"trigger": "caller_request", "action": "transfer_to_human"}]
                        if kind == "voice"
                        else [],
                        max_call_seconds=600,
                        created_by=str(SEED_ACTOR),
                    )
                )
                await s.execute(
                    agents.update()
                    .where(agents.c.tenant_id == tenant.id, agents.c.id == agent_id)
                    .values(status="active", current_version=1)
                )
