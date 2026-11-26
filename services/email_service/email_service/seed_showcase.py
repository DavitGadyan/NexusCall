"""LOCAL DEVELOPMENT showcase data (see `nexus_core.devshowcase`): three Acme email campaigns (two completed, one
running) with their templates and per-recipient delivery outcomes, plus a second sender and a sending domain
still waiting for DNS verification.

Idempotent (fixed ids, ON CONFLICT DO NOTHING; template versions are written only with their template). Runs as
`email_app`, so RLS applies. Every message is in a provider-final state (delivered, bounced, complained,
unsubscribed) and no campaign is put in `campaign_schedule_index`, so the scheduler and the send worker never
touch them: seeding sends nothing.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.dialects.postgresql import insert

from email_service.content import prepare_template, render_template
from email_service.providers.ses import fake_dkim_tokens
from email_service.security import address_hash
from email_service.tables import (
    email_campaigns,
    email_messages,
    sender_identities,
    sending_domains,
    template_versions,
    templates,
    usage_counters,
)
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import demo_id
from nexus_core.devshowcase import CAMPAIGN_MANAGER, EMAIL_FROM, EMAIL_REPLY_TO, EMAIL_SENDER_ID, TENANT, Scenario, sid

SEED_ACTOR = Actor("system", "dev-showcase")
DOMAIN_ID = sid("email_domain", 1)  # a second domain, DNS records not published yet
BILLING_SENDER_ID = sid("email_sender", 1)
ACME_DOMAIN_ID = demo_id("email_domain", 1)  # email_service.seed: the verified acme.test domain

# key -> (name, subject, html)
TEMPLATES: dict[str, tuple[str, str, str]] = {
    "ac_check": (
        "End-of-summer AC check",
        "Give your AC a little care before winter, {{first_name}}",
        "<h1>Hi {{first_name}},</h1><p>Your air conditioner worked hard this summer. A quick end-of-season "
        "check cleans the coils, tests the refrigerant and makes sure it starts right up next spring.</p>"
        '<p><a href="https://www.example.com/book">Book an AC check</a> or call us at (212) 555-0100.</p>'
        "<p>{{sender_name}}</p>",
    ),
    "tuneup": (
        "Furnace tune-up season",
        "Is your furnace ready for winter, {{first_name}}?",
        "<h1>Hi {{first_name}},</h1><p>Cold nights are on the way. A yearly tune-up keeps your furnace safe, "
        "efficient and quiet, and catches small problems before they turn into a no-heat emergency.</p>"
        "<p>A standard visit is $89 and includes the first 30 minutes of labor.</p>"
        '<p><a href="https://www.example.com/book">Book your tune-up</a> or call us at (212) 555-0100.</p>'
        "<p>Stay warm,<br>{{sender_name}}</p>",
    ),
    "pipes": (
        "Winter pipe-freeze tips",
        "5 ways to keep your pipes from freezing",
        "<h1>Hi {{first_name}},</h1><p>A burst pipe is one of the most expensive surprises of the winter. Five "
        "simple habits help:</p><ol><li>Keep the heat at 55&deg;F or above, even when you're away.</li>"
        "<li>Open cabinet doors under sinks on outside walls.</li><li>Let a faucet drip on the coldest nights."
        "</li><li>Disconnect garden hoses and shut off outdoor spigots.</li><li>Know where your main water "
        "shut-off valve is.</li></ol><p>If a pipe does freeze, call us at (212) 555-0100.</p>"
        "<p>{{sender_name}}</p>",
    ),
    "water_heater": (
        "Water heater holiday savings",
        "Holiday savings on a new water heater, {{first_name}}",
        "<h1>Hi {{first_name}},</h1><p>Hosting family this holiday season? Make sure there is enough hot "
        "water to go around. Through December 31st, installation quotes are free and we will haul away your old "
        "tank at no charge.</p>"
        '<p><a href="https://www.example.com/water-heaters">Request your free quote</a></p>'
        "<p>Happy holidays,<br>{{sender_name}}</p>",
    ),
}


async def seed(db: Database, sc: Scenario) -> None:
    by = f"user:{CAMPAIGN_MANAGER}"
    first = min(c.created_at for c in sc.email_campaigns) - timedelta(days=20)
    async with db.tenant_session(TENANT.id, SEED_ACTOR) as s:
        await s.execute(
            insert(sending_domains)
            .values(id=DOMAIN_ID, tenant_id=TENANT.id, domain="mail.acmehome.example", status="pending",
                    dkim_tokens=fake_dkim_tokens("mail.acmehome.example"), created_by=by, created_at=first,
                    updated_at=first)  # fmt: skip
            .on_conflict_do_nothing()
        )
        await s.execute(
            insert(sender_identities)
            .values(id=BILLING_SENDER_ID, tenant_id=TENANT.id, domain_id=ACME_DOMAIN_ID,
                    from_name="Acme Home Services billing", from_address="billing@acme.test",
                    reply_to="billing@acme.test", created_by=by, created_at=first, updated_at=first)  # fmt: skip
            .on_conflict_do_nothing()
        )
        sent_per_day: dict[date, int] = {}
        for i, (key, (name, subject, html)) in enumerate(TEMPLATES.items(), start=1):
            camp = next(c for c in sc.email_campaigns if c.template_key == key)
            created = camp.created_at - timedelta(days=1)
            clean = prepare_template(category="campaign", subject=subject, html=html, text=None)
            made = (
                await s.execute(
                    insert(templates)
                    .values(id=sid("email_template", i), tenant_id=TENANT.id, name=name, category="campaign",
                            created_by=by, created_at=created, updated_at=created)  # fmt: skip
                    .on_conflict_do_nothing()
                    .returning(templates.c.id)
                )
            ).scalar_one_or_none()
            if made is not None:
                await s.execute(
                    insert(template_versions).values(
                        id=sid("email_template_version", i),
                        tenant_id=TENANT.id,
                        template_id=sid("email_template", i),
                        version=1,
                        subject=clean.subject,
                        html=clean.html,
                        text=clean.text,
                        variables=clean.variables,
                        created_by=by,
                        created_at=created,
                    )
                )
            done = camp.completed_at
            last = max((m.state_changed_at for m in camp.messages), default=camp.launched_at)
            await s.execute(
                insert(email_campaigns)
                .values(
                    id=camp.id,
                    tenant_id=TENANT.id,
                    name=camp.name,
                    sender_id=EMAIL_SENDER_ID,
                    template_id=sid("email_template", i),
                    template_version=1,
                    segment_id=camp.segment_id,
                    state=camp.state,
                    timezone=TENANT.timezone,
                    max_per_minute=camp.max_per_minute,
                    launched_at=camp.launched_at,
                    launched_by=by,
                    started_at=camp.launched_at + timedelta(seconds=20),
                    completed_at=done,
                    snapshot_done=True,
                    created_by=by,
                    version=4 if done else 3,
                    created_at=camp.created_at,
                    updated_at=done or last,
                )
                .on_conflict_do_nothing()
            )
            for m in camp.messages:
                c = m.contact
                values = {"first_name": c.first, "last_name": c.last, "full_name": c.name,
                          "sender_name": TENANT.name}  # fmt: skip
                body = render_template(category="campaign", subject=clean.subject, html=clean.html, text=clean.text,
                                       values=values, unsubscribe_url=None)  # fmt: skip
                await s.execute(
                    insert(email_messages)
                    .values(
                        id=m.id,
                        tenant_id=TENANT.id,
                        category="campaign",
                        state=m.state,
                        campaign_id=camp.id,
                        contact_id=c.id,
                        sender_id=EMAIL_SENDER_ID,
                        template_id=sid("email_template", i),
                        template_version=1,
                        from_address=EMAIL_FROM,
                        from_name=TENANT.name,
                        reply_to=EMAIL_REPLY_TO,
                        to_address=c.email,
                        to_hash=address_hash(c.email),
                        subject=body.subject,
                        body_text=body.text,
                        body_html=body.html,
                        render_vars=values,
                        rfc_message_id=f"<{m.id}@acme.test>",
                        idempotency_key=f"campaign:{camp.id}:{c.id}",
                        provider="ses",
                        provider_message_id=f"0100019{m.id.hex[:9]}-{m.id.hex[9:17]}-000000",
                        source_type="campaign",
                        source_id=str(camp.id),
                        attempts=1,
                        suppression_reason=m.suppression_reason,
                        suppression_synced_at=m.state_changed_at + timedelta(seconds=30) if m.suppression_reason
                        else None,
                        queued_at=m.queued_at,
                        accepted_at=m.accepted_at,
                        delivered_at=m.delivered_at,
                        state_changed_at=m.state_changed_at,
                        created_at=m.queued_at - timedelta(seconds=5),
                        updated_at=m.state_changed_at,
                    )  # fmt: skip
                    .on_conflict_do_nothing()
                )
                day = m.accepted_at.date()
                sent_per_day[day] = sent_per_day.get(day, 0) + 1
        for tx in sc.tx_emails:  # booking confirmations and approved AI replies
            c = tx.contact
            await s.execute(
                insert(email_messages)
                .values(
                    id=tx.id,
                    tenant_id=TENANT.id,
                    category=tx.category,
                    state="delivered",
                    contact_id=c.id,
                    from_address=tx.from_address,
                    from_name=f"{TENANT.name} support" if tx.category == "reply" else TENANT.name,
                    reply_to=EMAIL_REPLY_TO if tx.category == "transactional" else None,
                    to_address=c.email,
                    to_hash=address_hash(c.email),
                    subject=tx.subject,
                    body_text=tx.body,
                    rfc_message_id=f"<{tx.id}@acme.test>",
                    idempotency_key=str(tx.id),
                    provider="ses",
                    provider_message_id=f"0100019{tx.id.hex[:9]}-{tx.id.hex[9:17]}-000000",
                    source_type=tx.source_type,
                    source_id=tx.source_id,
                    source_version=1 if tx.category == "reply" else None,
                    attempts=1,
                    queued_at=tx.accepted_at - timedelta(milliseconds=600),
                    accepted_at=tx.accepted_at,
                    delivered_at=tx.delivered_at,
                    state_changed_at=tx.delivered_at,
                    created_at=tx.accepted_at - timedelta(seconds=1),
                    updated_at=tx.delivered_at,
                )
                .on_conflict_do_nothing()
            )
            sent_per_day[tx.accepted_at.date()] = sent_per_day.get(tx.accepted_at.date(), 0) + 1
        for day, sent in sorted(sent_per_day.items()):
            stmt = insert(usage_counters).values(tenant_id=TENANT.id, day=day, sent=sent)
            await s.execute(stmt.on_conflict_do_nothing())
