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
| `media/thumbnail-b.jpg` | Upwork thumbnail (recommended) |
| `media/thumbnail-a.jpg` | Alternative thumbnail |
| `media/nexuscall-demo.mp4` | 1.5-minute demo video with music and captions |
| `screenshots/*.png` | Product screenshots, in story order |

Note: the screenshots and video use a demonstration workspace ("Acme Home Services") with sample data. The product, its features and the interface are real.
