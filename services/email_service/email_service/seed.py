"""LOCAL DEVELOPMENT demo data, per demo tenant: a verified sending domain (`acme.test` / `globex.test`, so
email-inbox replies from `support@<tenant>.test` pass the domain check), a sender identity, a campaign
template and a draft campaign over the crm demo segment. Idempotent (fixed ids, ON CONFLICT DO NOTHING);
runs as the restricted `email_app` role, so RLS applies. Nothing is sent by seeding.
"""

from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from email_service.content import prepare_template
from email_service.providers.ses import fake_dkim_tokens
from email_service.tables import email_campaigns, sender_identities, sending_domains, template_versions, templates
from nexus_core.context import Actor
from nexus_core.db import Database
from nexus_core.devseed import ACME, GLOBEX, demo_id

SEED_ACTOR = Actor("system", "dev-seed")
TENANTS = ((ACME, 1, "acme.test"), (GLOBEX, 101, "globex.test"))
HTML = (
    "<h1>Hello {{first_name}},</h1>"
    "<p>Spring is here: book your seasonal check-up with {{sender_name}}.</p>"
    '<p><a href="https://www.example.com/book">Book a visit</a></p>'
)


async def seed(db: Database) -> None:
    for tenant, n, domain in TENANTS:
        clean = prepare_template(category="campaign", subject="A spring check-up, {{first_name}}?", html=HTML,
                                 text=None)  # fmt: skip
        domain_id, sender_id = demo_id("email_domain", n), demo_id("email_sender", n)
        template_id = demo_id("email_template", n)
        async with db.tenant_session(tenant.id, SEED_ACTOR) as s:
            await s.execute(
                insert(sending_domains)
                .values(
                    id=domain_id,
                    tenant_id=tenant.id,
                    domain=domain,
                    status="verified",
                    dkim_tokens=fake_dkim_tokens(domain),
                    dkim_status="pass",
                    spf_status="pass",
                    dmarc_status="pass",
                    created_by=str(SEED_ACTOR),
                )
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(sender_identities)
                .values(
                    id=sender_id,
                    tenant_id=tenant.id,
                    domain_id=domain_id,
                    from_name=tenant.name,
                    from_address=f"news@{domain}",
                    reply_to=f"support@{domain}",
                    created_by=str(SEED_ACTOR),
                )
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(templates)
                .values(
                    id=template_id,
                    tenant_id=tenant.id,
                    name="Spring check-up",
                    category="campaign",
                    created_by=str(SEED_ACTOR),
                )  # fmt: skip
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(template_versions)
                .values(
                    tenant_id=tenant.id,
                    template_id=template_id,
                    version=1,
                    subject=clean.subject,
                    html=clean.html,
                    text=clean.text,
                    variables=clean.variables,
                    created_by=str(SEED_ACTOR),
                )
                .on_conflict_do_nothing()
            )
            await s.execute(
                insert(email_campaigns)
                .values(
                    id=demo_id("email_campaign", n),
                    tenant_id=tenant.id,
                    name="Spring check-up (demo)",
                    sender_id=sender_id,
                    template_id=template_id,
                    segment_id=demo_id("segment", n),
                    timezone=tenant.timezone,
                    max_per_minute=30,
                    created_by=str(SEED_ACTOR),
                )
                .on_conflict_do_nothing()
            )
