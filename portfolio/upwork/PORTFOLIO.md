# Upwork portfolio entry: NexusCall

## Project title (Upwork limit: 70 characters)

**NexusCall: Multi-Tenant AI Voice Call Center & CRM Platform**

(58 characters)

Alternative titles:
- AI Call Center SaaS: Voice Agents, CRM, Email & Admin Consoles (62)
- AI Voice Agent Platform with CRM, Campaigns & Human-Approved Email (66)

## Your role

Solution architect and full-stack engineer: architecture, backend microservices, AI integration, security, web app, infrastructure.

## Project description (Upwork limit: 600 characters)

> A multi-tenant SaaS platform where AI voice agents make and answer phone calls, qualify leads and book appointments, with a CRM, call and email campaigns, and AI-drafted email replies that a person approves before they are sent. Built as 16 Python microservices with a Next.js web app, an AI gateway hardened against the OWASP Top 10 for LLMs, per-tenant data isolation in PostgreSQL, secrets in HashiCorp Vault, and Kubernetes deployment. Integrates Twilio, Amazon SES, Anthropic Claude, Deepgram and ElevenLabs.

(about 560 characters)

## Skills / tags

AI Agent Development · Python · FastAPI · Next.js · LLM Integration · Twilio · Microservices · Kubernetes · PostgreSQL · Voice AI

## Success criteria achieved

All of these are true of the delivered codebase. They describe what was built and verified, not business results from a live customer.

- **Complete AI call-center product:** inbound and outbound AI voice calls (Twilio, Deepgram speech-to-text, Anthropic Claude, ElevenLabs text-to-speech), with barge-in, AI disclosure, recording consent and live hand-off to a human.
- **16 independent microservices:** each has its own database, migrations, API, workers and tests. They communicate through RabbitMQ with the outbox/inbox pattern, so no event is lost or processed twice.
- **Strict tenant isolation:** PostgreSQL row-level security is forced on every tenant table. Automated cross-tenant tests check that one tenant can never read, change, export or search another tenant's data.
- **AI gateway hardened against the OWASP Top 10 for LLM applications:** prompt-injection detection, tool-call authorization, data-classification limits, budgets, kill switches and an audit trail. 40 dedicated adversarial tests.
- **Human in the loop for email:** AI drafts replies to inbound email. Nothing is sent until a person with the right permission approves it, and tests prove that an unapproved draft cannot be sent.
- **Enterprise security:** Keycloak single sign-on with MFA step-up for sensitive actions, a backend-for-frontend so tokens never reach the browser, HashiCorp Vault for every provider key, and a tamper-evident audit log.
- **Three role-based interfaces:** an agency workspace, a tenant admin console and a platform admin console, with 99 web routes.
- **Quality:** about 1,400 automated tests (1,085 Python tests against real PostgreSQL, RabbitMQ and Redis, plus 307 web tests). Type checking and linting are clean.
- **Production-ready deployment:** Helm chart (network policies, restricted pod security, migration jobs, Vault agent injection), Terraform for cluster add-ons, k6 load tests, runbooks and a compliance checklist.

## Media

| File | Use |
|---|---|
| `media/thumbnail-owasp.jpg` | **Upwork thumbnail (recommended):** laptop, AI Gateway shield, "OWASP LLM Top 10 Protected" badge |
| `media/thumbnail-b.jpg`, `media/thumbnail-a.jpg` | Alternative thumbnails |
| `media/nexuscall-demo.mp4` | 86-second demo video, 1920x1080, with music and captions |
| `media/shield-badge.png` | Transparent AI Gateway / OWASP badge |
| `screenshots/*.png` | 19 product screenshots (3200x2000), in story order |

### Screenshot captions (paste into Upwork in this order)

1. **01-workspace-dashboard:** Live dashboard: calls answered, qualified leads, appointments, AI replies and cost, with 30-day trends.
2. **02-call-campaigns:** Outbound AI call campaigns with calling windows, pacing and a workspace kill switch.
3. **03-call-campaign-detail:** A running campaign: audience, progress and call outcomes.
4. **04-call-history:** Every inbound and outbound call, with masked phone numbers and outcomes.
5. **05-call-detail-transcript:** Call timeline and AI conversation transcript. Opening it is recorded in the audit log.
6. **06-bookings-calendar:** Appointments booked by the AI agent; double bookings are rejected.
7. **07-crm-leads:** Built-in CRM with lead scores, status and sources.
8. **08-ai-agents:** Voice and email AI agents with versioned prompts and tool grants.
9. **09-inbox:** Shared inbox: inbound email is classified and AI reply drafts are prepared.
10. **10-inbox-ai-draft-review:** Approval queue: a person approves every AI-written email before it is sent.
11. **11-email-campaigns:** Email campaigns with delivery, bounce and complaint reporting.
12. **12-analytics:** Analytics: call, email, AI-draft and booking funnels, and costs.
13. **13-tenant-ai-settings-usage:** Per-client AI settings: allowed models, budget, data classification.
14. **14-platform-overview:** Platform console: all client workspaces, usage and cost by tenant.
15. **15-platform-tenants:** Multi-tenant directory of client workspaces.
16. **16-platform-usage:** Usage and cost per tenant: telephony, AI and email.
17. **17-ai-gateway-owasp-security:** AI Gateway security events: prompt injection (OWASP LLM01) and denied tools (LLM03, LLM06), detected live.
18. **18-security-rls-coverage:** Row-level security coverage scan across all 16 service databases.
19. **19-security-vault-secrets:** Provider credentials are stored in HashiCorp Vault; only metadata is shown.

Note: the screenshots and the video use a demonstration workspace ("Acme Home Services") and fictional clients with sample data. The product, its features and its interface are real. The security events in screenshot 17 include prompt-injection attacks sent to the live AI Gateway during recording.
