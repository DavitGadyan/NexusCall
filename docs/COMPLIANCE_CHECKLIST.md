# NexusCall — Compliance checklist (engineering controls for counsel review)

> **This is not legal advice and not a certification of compliance.** It maps the technical controls NexusCall provides to the obligations they are meant to support, says where each one is implemented and what evidence exists, and lists the questions **qualified counsel** must answer for each operating and recipient jurisdiction. Whether a deployment complies depends on how each tenant configures and uses the platform, on contracts (DPAs, provider terms) and on the law where the callers and recipients are. NexusCall makes no compliance claim (build.md §11).

Status legend:
- **Built**: implemented and covered by automated tests in the owning service.
- **Configurable**: a control exists, and the tenant or platform must set it correctly.
- **Gap**: not implemented yet, or needs a process outside the software.

## 1. Cross-cutting controls

| Control | Where implemented | Evidence | Status |
|---|---|---|---|
| Tenant isolation (forced RLS on every tenant table, non-owner runtime roles) | `nexus_core.rls`, every service's migrations; `tests/security/test_migrations.py`; production DB roles NOSUPERUSER/NOBYPASSRLS (`charts/platform-data`, `vault-config` database engine) | migration tests, cross-tenant negative tests, Platform → Security RLS coverage scan | Built |
| Least-privilege secrets; no secrets in DB, queues, logs | Vault policies per service (`infra/vault/bootstrap.sh` → `vault-config`), `CredentialResolver`, log redaction | Vault audit device, log-redaction tests | Built |
| Encryption in transit | TLS at the edge (Traefik, TLS ≥ 1.2), PostgreSQL `hostssl` only, Vault TLS | rendered manifests, `pg_hba` | Configurable / **Gap**: Redis, AMQP and service-to-service HTTP inside the cluster are plaintext behind NetworkPolicies (DEPLOYMENT §10) |
| Encryption at rest | Storage-class / disk encryption, S3 server-side encryption, Vault barrier | cluster and storage configuration | Configurable (infrastructure owner) |
| Audit trail (append-only, hash-chained) | audit-service; `emit_audit` in every service; Vault and Kubernetes audit logs | `POST /api/v1/platform/audit/verify`, audit export | Built |
| Privileged access (step-up MFA, support grants, break-glass) | tenant-service support grants, `require_step_up`; [break-glass runbook](runbooks/break-glass.md) | exercise records | Built + process |
| Retention limits | Tenant retention settings within platform maxima (`settings_schema.py`); per-service retention jobs | settings audit, job logs | Configurable / partial: some retention jobs are still per-service work in progress |
| Data subject rights (export, correction, deletion, restriction) | CRM edit/export; audited exports | — | **Gap**: there is no end-to-end erasure or restriction workflow across all services yet |
| Data residency and transfers | Single-region deployment; provider selection per tenant | deployment record | Configurable / counsel |
| Backups and restore | CNPG PITR, Vault snapshots, object replication ([runbook](runbooks/backup-restore.md)) | drill logs | Built + process |
| Incident response and breach handling | [secret rotation and compromise](runbooks/secret-rotation.md), kill switches | incident records | Process |
| AI governance (no auto-send, trust-labelled untrusted content, tool risk classes, budgets, kill switches) | ai-gateway, mcp-registry, tool-execution; `TenantAi.draft_auto_send = False` (locked) | OWASP LLM assessment (`docs/security/OWASP_LLM_TOP10_ASSESSMENT.md`), injection test corpus | Built |

## 2. Jurisdiction-specific obligations
For each row: the control → where it is implemented → evidence → **open legal questions**.

### 2.1 TCPA and US telemarketing rules (outbound calls, United States)
| Control | Where | Evidence | Open questions for counsel |
|---|---|---|---|
| Consent records with source, timestamp, policy version and evidence reference | crm-service `consent_records` (append-only) | consent API, audit events | What consent standard applies to each tenant's use case (prior express vs. prior express *written* consent for calls using an artificial or prerecorded voice)? Is the captured evidence sufficient? |
| AI / automated-call disclosure at call start | voice-service disclosure prompt; tenant `ai_disclosure_enabled` | call transcripts | Does an AI voice count as an "artificial or prerecorded voice" for each campaign type? Required disclosure wording and timing? |
| Calling windows in the **contact's** timezone | campaign dispatcher + call-service pre-dial gate | state-machine tests | Which hours apply (federal and state rules, more restrictive states)? Holidays? |
| Do-not-call: internal suppression; opt-out honoured across channels | crm suppressions; `call.opted_out` → suppression; `honor_opt_out_across_channels` locked on | opt-out tests | National DNC registry scrubbing is **not built** (Gap). How often must the registry be scrubbed? How fast must opt-outs take effect? |
| Caller ID / number ownership | inbound numbers managed per tenant | number records | Are STIR/SHAKEN attestation and caller-ID rules met by the telephony provider's configuration? |
| Country allowlist, concurrency and daily caps | tenant communication settings, platform limits | settings audit | Is the tenant allowed to call each listed country? |

### 2.2 GDPR / UK GDPR and ePrivacy (EU/UK data subjects)
| Control | Where | Evidence | Open questions for counsel |
|---|---|---|---|
| Lawful basis recorded per consent record | crm `consent_records.lawful_basis` | consent API | Which basis does each processing purpose use (consent vs. legitimate interest), per tenant? Does ePrivacy require prior consent for automated calls and marketing email in each member state? |
| Controller/processor roles | — | contracts | Is NexusCall (the agency) a processor for each tenant? Are DPAs and sub-processor lists in place (Twilio, AWS SES, Anthropic, Deepgram, ElevenLabs, Google, the hosting provider)? |
| Data minimisation in logs, events and prompts | `nexus_core.logging` redaction; IDs-only events; PII minimisation in ai-gateway | redaction tests | Is sending call transcripts or email content to the LLM, STT and TTS providers compatible with the stated purposes? Do transfers outside the EEA or UK need SCCs or a transfer impact assessment? |
| Retention per data class | tenant retention settings | settings | What retention periods are justified per class (recordings, transcripts, email bodies, attachments, audit)? |
| Data subject rights | partial (see §1) | — | **Gap** for erasure and restriction across services. What response times and verification are required? |
| Records of processing, DPIA | — | — | Is a DPIA required (large-scale AI voice processing, recordings)? Who maintains the records of processing? |
| Breach notification | incident runbooks | incident records | Notification thresholds and timelines (for example 72 h to the supervisory authority) and who notifies (controller vs. processor) |

### 2.3 CAN-SPAM (commercial email, United States)
| Control | Where | Evidence | Open questions for counsel |
|---|---|---|---|
| Working unsubscribe in every marketing email (RFC 8058 one-click plus a visible link), honoured promptly | email-service `/u/{token}`, `List-Unsubscribe` headers, suppression on unsubscribe | unsubscribe tests | Is the processing time short enough (the law sets a maximum number of business days)? |
| Accurate header and sender identity; verified domains only | email-service domains and senders (SPF/DKIM/DMARC status) | domain verification records | Do subject lines and "from" names meet the non-deception requirements? That is a tenant content responsibility |
| Valid physical postal address in marketing mail | email templates | — | **Configurable**: the tenant must include it. Should the platform enforce a template field? |
| Separation of transactional and marketing mail | separate categories, queues and policies | config | Which messages count as "transactional or relationship" for each tenant? |

### 2.4 CASL (commercial electronic messages, Canada)
| Control | Where | Evidence | Open questions for counsel |
|---|---|---|---|
| Consent evidence (express or implied, with source and date) | crm consent records | consent API | Are the implied-consent windows (existing business relationship, inquiries) tracked and expired correctly? Currently **Gap**: no automatic expiry of implied consent |
| Sender identification and contact information; unsubscribe within the legal deadline | email templates + unsubscribe | tests | Required identification content per message; the unsubscribe processing deadline |
| Record-keeping of consent | append-only consent records + audit | audit export | How long must the evidence be kept? |

### 2.5 Call recording and transcription consent (all jurisdictions)
| Control | Where | Evidence | Open questions for counsel |
|---|---|---|---|
| Configurable recording and consent prompt | tenant `recording_enabled`, `recording_consent_prompt`; voice-service plays the prompt at call start | transcripts and recordings metadata | Which recipient locations require **all-party** consent? Is a prompt enough, or is an explicit "yes" needed? What if the caller declines: stop recording, or end the call? |
| Access control on recordings and transcripts | `calls:recordings:read`, audited access, presigned URLs with a 5-minute expiry | audit events | Who may access recordings? What retention is needed? Is a legal-hold process needed? |
| Retention purge | voice-service retention job (tenant setting, 7–730 days) | job logs | Retention limits per jurisdiction and per purpose |

## 3. Before the first production tenant
- [ ] Counsel has answered the open questions above for every operating and recipient jurisdiction, and the tenant contract states the tenant's responsibilities (consent collection, content, DNC registry scrubbing where applicable).
- [ ] DPAs and sub-processor agreements signed. Transfer mechanisms in place where needed.
- [ ] Provider configuration reviewed: Twilio caller ID and attestation; SES production access and configuration sets; Anthropic data-use settings (no training on customer data; retention).
- [ ] The gaps marked **Gap** above have accepted owners and dates, or documented risk acceptance.
- [ ] A backup/restore drill, a break-glass exercise and a kill-switch drill are recorded ([runbooks](runbooks/)).
- [ ] A penetration test of the admin UI and an adversarial AI test are done (build.md §23 Phase 8). Findings triaged.

This checklist is an engineering artefact. Review it with counsel at every phase and whenever a new jurisdiction, channel or provider is added.
