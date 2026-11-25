# Brief: web pages for CRM, call campaigns, AI agents and knowledge

**Route folders you own** (under the workspace shell): `app/crm/**`, `app/campaigns/calls/**`, `app/agents/**`, `app/knowledge/**`.
**Components:** `components/crm/**`, `components/campaigns/**`, `components/agents/**`.
**Contracts:** `crm_service.json`, `campaign_service.json`, `ai_agent_service.json`, `mcp_registry.json` (tools and grants).

## Pages
- **Leads and contacts**
  - Tables with filters, sort and search.
  - Detail page: profile, custom fields, tags, consent history, suppression state, notes, activity.
  - Create and edit.
  - Suppress or unsuppress. Unsuppress needs step-up.
  - Export, as an audited async job with a download.
- **Imports**
  - CSV upload with a column preview.
  - Progress.
  - Result summary, and download of the per-row report.
- **Segments**
  - Rule builder for the DSL: fields and operators from the allowlist, nested AND/OR.
  - Live preview count.
- **Custom fields and tags** management.
- **Call campaigns**
  - List with state badges.
  - Builder: agent, segment, timezone, calling window, pacing, attempts. Validate against the tenant limits.
  - Schedule, launch (with a confirmation), pause, resume, cancel.
  - Report: counts by state and outcome.
  - Tenant kill switch, with a typed confirmation and step-up.
- **AI agents**
  - List and detail.
  - Prompt versions shown as an immutable history, with a diff view.
  - Create a version, activate a version.
  - Tool grants: the tool catalogue with risk classes. Class 3 shows "always needs approval". Class 4 cannot be granted.
  - Evaluations: datasets, cases, run results.
- **Knowledge**
  - Sources with a classification.
  - Document upload with status.
  - A search test box that shows chunks and scores.
