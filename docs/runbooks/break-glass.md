# Runbook: break-glass exercise (support access and emergency infrastructure access)

There are two kinds of emergency access. Neither is a hidden super-admin. Both are time-limited, reason-coded, notified, audited and reviewed (build.md §20.3).

| Level | What | Mechanism |
|---|---|---|
| **A. Tenant support access** | Read-only access to one tenant's data for a support case | tenant-service support grants: request → second-admin approval → expiry ≤ platform `support_grant_max_hours` (default 4 h). Every read under the grant is audited |
| **B. Infrastructure break-glass** | Cluster or Vault admin when the normal path is down (Vault sealed, Keycloak down, operator lock-out) | Sealed envelope / offline credentials under split custody, used only with an incident ticket |

There is **no** path that disables RLS or grants `BYPASSRLS`. Cross-tenant reads use only the `<db>_platform` roles, and only where an active support grant's policy allows them.

## A. Tenant support access (exercise every quarter, staging)
Participants: requester (platform operator), approver (a different platform owner), observer (security), and the tenant owner of a demo tenant.
1. **Request** (Platform → Security → Break-glass, or `POST /api/v1/platform/support-grants`):
   - `scope` (`read_config` | `read_operations` | `read_crm`)
   - `reason` (≥ 10 characters)
   - `ticket_ref`
   - `duration_minutes`

   Step-up MFA is required. **Check:** the request is refused without step-up. The tenant owner gets the `support_access_requested` notification.
2. **Self-approval is refused.** The requester tries `POST …/support-grants/{id}/decision` with approve: **expect 403**.
3. **Approve.** A second platform owner, with step-up, approves. **Check:** status `active`, `support.access_granted` in the audit log, and the tenant owner is notified.
4. **Use.** The requester reads only within the scope. For example, `read_operations` allows calls and campaigns but not contacts. **Check:**
   - Reads outside the scope return 403.
   - Every read is in the audit log with the grant id.
   - Writes and exports are impossible: support scopes are read-only by construction (`SUPPORT_SCOPES`).
5. **Expiry.** Let it lapse, or set `duration_minutes=15` for the exercise. The `expire_support_grants` periodic job marks it `expired` and the next request returns 403. **Check:** `support.access_expired` is audited, with no manual action.
6. **Early revoke.** Repeat with a new grant and revoke it: `support.access_revoked`, and access ends immediately (the authz epoch is bumped).
7. **Review.**
   - Platform → Audit: export every `support.*` event for the exercise.
   - The security observer signs off that the reason, ticket, scope, approver and notifications are all present.
   - Production grants are reviewed weekly in the same way.

## B. Infrastructure break-glass (exercise twice a year)
Material, kept offline and split between two custodians:
- Vault unseal or recovery key shares
- a sealed cluster-admin kubeconfig for a dedicated `breakglass` identity, normally disabled in the IdP or cloud IAM
- the Keycloak master-realm admin credential

**Procedure (also the exercise):**
1. Open an incident ticket. Name the incident commander and the two custodians. Note the start time.
2. Enable the break-glass identity, for example by enabling the IdP account or attaching the role. Using it alerts security automatically: there is an alert rule on that identity's audit events.
3. Do only the minimum fix, for example unseal Vault or restore Keycloak admin. A second person watches every command, and a shell transcript is recorded (`script` command) and attached to the ticket.
4. Disable the identity again. Rotate whatever was exposed:
   - new kubeconfig credentials
   - Keycloak master admin password
   - if the root token was regenerated: `vault operator generate-root`, used, then `vault token revoke -self`
5. **Review within two business days.** What happened, why the normal path failed, every action taken (Kubernetes audit log, Vault audit log, Keycloak admin events), and follow-up tasks. The exercise passes only if all three logs show the actions and the identity is disabled again.

## Evidence checklist
- [ ] Support grant: request, refused self-approval, approval, scoped reads, expiry and revoke, all in the audit export
- [ ] Notifications delivered to the tenant owner and to security
- [ ] Break-glass identity disabled at the end; credentials rotated
- [ ] Review notes filed with the change record
