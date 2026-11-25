# Brief: web pages for integrations, analytics, AI settings and platform operations

**Route folders you own:** `app/integrations/**`, `app/analytics/**`. Under the tenant admin shell: `admin/connections/**`, `admin/ai/**`. Under the platform shell: `platform/ai/**`, `platform/infra/**`, `platform/integrations/**`, `platform/usage/**`, `platform/security/secrets/**`.
**Components:** `components/integrations/**`, `components/analytics/**`, `components/platform-ops/**`.
**Contracts:** `integration_service.json`, `analytics_service.json`, `ai_gateway.json`, `mcp_registry.json`, and `tenant_service.json` (the `/api/v1/platform/infra/*` paths).

Invoke the `dataviz` skill before you build any chart.

## Pages
- **Provider connections** (tenant):
  - A wizard: choose the provider, enter credentials in a write-only form, run the server-side test, see the status.
  - List: masked account, last validated, rotation due, health.
  - Test, rotate, disable. Revoke needs step-up and a typed confirmation.
  - OAuth connect, for calendar and CRM.
  - The UI never shows a stored secret.
- **Webhooks**: endpoints, event types, delivery log with retries.
- **Analytics**: overview (call funnel, email funnel, bookings, AI draft outcomes, costs), time series, campaign drill-down. Exports as an audited async job with a download.
- **Tenant AI settings**: allowed models, budget and usage, data classification, approval rules. Platform-locked controls are shown as locked.
- **Platform AI gateway**:
  - Providers and models: enable or disable, prices.
  - Routing.
  - Tenant allowlists.
  - Kill switches, with step-up and a typed confirmation.
  - Security events.
  - MCP servers and the tool catalogue: risk class, enable or revoke.
  - Policy proposals with dual approval and history.
- **Platform infrastructure**:
  - Services and health.
  - Queues and workers.
  - Dead letters: a metadata-only view. Replay and discard need step-up and a reason.
- **Secrets health**: references and rotation status only.
- **Platform integrations**.
- **Usage**: per tenant, with costs.
