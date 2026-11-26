# Runbook: kill switches

Kill switches stop outbound activity **immediately** without a deploy (build.md §11, §24.1). They are configuration in the owning services' databases. Every change requires step-up MFA and is audited. Prefer the narrowest switch that stops the harm.

| Switch | Who | How | Effect |
|---|---|---|---|
| Tenant: calls | Tenant admin (`campaigns:manage`) | UI: Campaigns → Kill switch, or `POST /api/v1/campaigns/kill-switch` `{engaged, reason}` | campaign dispatch stops at the next tick and `campaign.kill_switch` is emitted. call-service cancels queued calls. Pre-dial gates re-check `effective_calls_enabled` |
| Tenant: calls / emails (settings) | Tenant admin (`tenant:manage`) | Tenant admin → Communication → `calls_enabled` / `emails_enabled` | `effective_*_enabled` false for that tenant. Senders and dialers check it at execution time (5 s settings cache) |
| Platform: calls / emails / AI | Platform owner (`platform:settings:manage`, step-up) | Platform → Settings → Kill switches (`PUT /api/v1/platform/settings/kill_switches`) with a reason | Applies to **every** tenant through `effective_*_enabled` |
| AI gateway: platform / provider / model / tenant / agent | Platform owner (`platform:ai:manage`) | Platform → AI Gateway → Kill switches (`POST /api/v1/platform/ai/kill-switches` `{scope, target, engaged, reason}`) | Requests matching the scope are denied with `policy_denied: kill_switch` before any provider call |
| Tools / MCP servers | Platform owner | Platform → AI → Tools / MCP servers → disable or revoke | Tool execution is denied for the tool or server |
| Single campaign | Campaign manager | Campaign → Pause or Cancel | Only that campaign |

## When to pull which
- **Spam or abuse from one tenant** (complaint spike, toll fraud, compromised account): tenant calls or emails switch, then investigate. A platform admin can engage the platform switch if the tenant admin is unavailable or is the suspect. Also suspend the tenant (Platform → Tenants → Suspend).
- **Provider failure or runaway retries:** the platform switch for that channel ([provider outage](provider-outage.md)).
- **Model misbehaviour or prompt-injection incident:** an AI gateway switch at `agent` or `model` scope first. Use `platform` scope if the scope is unclear. AI email replies are drafts that need approval anyway. The voice path is the urgent one.
- **Legal hold or regulator request:** the platform switch, per counsel's instruction.

## Procedure
1. Engage the switch with a **reason** that names the incident ticket. Step-up is required.
2. Verify that it took effect:
   - Calls: `GET /api/v1/calls/live` drains and no new `initiating` rows appear (Grafana call funnel).
   - Email: the `emails.campaign_send` consumer rate drops to 0. Queued messages stay `queued`. They are not sent, and not lost.
   - AI: `ai_requests` show `policy_denied` with reason `kill_switch`.
3. Communicate: notify the tenant owner(s) and the support desk.
4. **Release:**
   1. Confirm the cause is fixed.
   2. Release the narrowest switch first.
   3. Watch the first minutes. Campaign pacing restarts within windows, and nothing is sent outside a calling or sending window.
5. Review the audit events (`campaign.kill_switch.*`, `platform.settings.updated` with `section=kill_switches`, AI gateway kill-switch events) and attach them to the incident.

## Drill (quarterly, staging, fake providers)
1. Start a small call campaign and an email campaign.
2. Engage the platform calls switch. Measure the time until the last new dial (target < 10 s).
3. Engage the tenant emails switch. Confirm no Mailpit deliveries after it.
4. Engage the AI `provider: anthropic` switch. Confirm voice turns use the fallback line.
5. Release all three. Confirm resumption, and that the audit trail is complete.
