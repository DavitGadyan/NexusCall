# ADR-0005: Every bounded context is a separate service from Phase 1

- Status: Accepted (user decision)
- Date: 2026-11-25

## Context
build.md lists 16+ bounded contexts and also warns against "empty microservices". The user chose full separation up front.

## Decision
We create all 16 services in Phase 1. Each one has a real bounded context, its own database, its own roles and its own deployment.

To respect the spec's warning:
- Until its phase arrives, a service exposes only health, readiness, metrics and its empty schema. It does not get placeholder business endpoints or fake domain logic.
- Each phase fills in its services completely: migrations, API, events, tests and UI.
- Shared code is limited to infrastructure packages (`nexus_core`, `event_schemas`, `provider_contracts`).

## Alternatives considered
- Staged consolidation into about 9 deployables: less operational load, but rejected by the user.

## Consequences
- Local resource cost is higher. Mitigated with Compose profiles (R1).
- Cross-service workflows rely on outbox/inbox and contract tests (ADR-0008).
