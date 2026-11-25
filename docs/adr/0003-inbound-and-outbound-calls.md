# ADR-0003: Support inbound and outbound calls in the MVP

- Status: Accepted (user decision)
- Date: 2026-11-25

## Decision
The MVP includes both call directions:
- **Outbound:** campaign-driven dialing through `calls.dispatch`.
- **Inbound:** a tenant-owned phone number routes to an AI agent.

Both directions share one voice pipeline (voice-service) and one call state machine (call-service), with a `direction` attribute on each call.

Inbound tenant resolution uses only the trusted `inbound_numbers` mapping (E.164 → tenant, agent). Nothing in the caller-supplied data can select the tenant.

## Consequences
- Phase 4 is larger. It adds an inbound number management UI, business-hours and overflow routing, and a human queue via Twilio conference.
- Number provisioning starts manually: the admin enters a Twilio number SID. Automated purchase is deferred.
