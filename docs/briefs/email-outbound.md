# Brief: email-service (campaigns and outbound delivery)

**Paths you own:** `services/email_service`, `services/fake_providers/fake_providers/routers/ses.py`.
**Database / app role:** `email` / `email_app`.
**build.md:** §3.1 K; §4.1; "Email deliverability and abuse controls"; §5.4; §6; §14 Phase 5.

## Tables
- `sending_domains`: verification, DKIM tokens, SPF and DMARC status.
- `sender_identities`
- `templates` and `template_versions` (append-only).
- `email_campaigns`: draft → scheduled → running ⇄ paused → completed | canceled | failed.
- `email_messages`: category campaign|transactional|reply. State pending → queued → sending → accepted → delivered | bounced | complained | rejected | unsubscribed | failed. Provider message id, RFC message id, idempotency key.
- `provider_events`: unique.
- `unsubscribe_tokens`: stored hashed.
- Usage counters.

## Domains, senders and templates
- Domains and senders (`emails:campaigns:manage`):
  - Adding a domain shows the DKIM records.
  - Verify with DNS (`dnspython`). The fake is deterministic.
  - Only verified domains can send.
- Templates:
  - Sanitize HTML with bleach.
  - Links must be http(s). No `javascript:` or `data:`, and no redirector.
  - Escape variables.
  - Reject CR and LF in headers.
  - Preview, and test send.

## Campaigns
- CRUD, schedule, pause, resume, cancel, report. Launch is idempotent and audited.
- Scheduler (`PeriodicJob`):
  1. Take the running campaigns that are inside their window.
  2. Page crm `segments/{id}/resolve` (channel `email`) and `contacts/batch`.
  3. Create message rows, unique per campaign and contact.
  4. Enqueue `emails.campaign_send` in controlled batches.

## Send task
1. Claim the message.
2. Check again: tenant `effective_emails_enabled`, crm suppression, and the token buckets (tenant daily quota, per domain, provider).
3. Render.
4. Send through the provider with an idempotency key.
5. Set the state to `accepted`.

- Permanent errors are not retried. Transient errors use bounded backoff with jitter.
- Marketing mail carries `List-Unsubscribe` and `List-Unsubscribe-Post` (RFC 8058), plus a visible link.

## Unsubscribe and provider events
- Unsubscribe: public `GET` / `POST /u/{token}`. POST is one-click. It emits `email.unsubscribed` and calls crm `suppressions`.
- `/webhooks/ses/events`:
  - Verify with `SnsVerifier`. Real: the default pattern and the configured topic ARNs. Fake mode: `cert_host_pattern=r"^fake-providers$", allow_http=True`.
  - Handle `SubscriptionConfirmation` safely.
  - Dedupe.
  - Map Delivery, Bounce, Complaint and Reject to states.
  - Permanent bounces and complaints become suppressions.
- Auto-pause a campaign when bounces exceed 5% or complaints exceed 0.1% (both configurable), and notify.

## Internal API, adapter, fake
- Internal API exactly as in conventions §5: `send` (IK) and message lookup.
- SES adapter:
  - Real: SES v2 `SendEmail` with raw MIME built by the stdlib `email` package, through aiobotocore. Credentials: `CredentialResolver.resolve(tenant, "ses", allow_platform_default=True)`.
  - Fake: HTTP to the simulator.
- Fake router `ses.py`:
  1. Accept sends idempotently.
  2. Relay the message to Mailpit over SMTP (`MAILPIT_SMTP_HOST`).
  3. Post signed SNS events (`fake_providers.sns.sign_sns`) to `EMAIL_EVENTS_WEBHOOK_URL`.

  Normally the event is Delivery. Recipients at `bounce.example.test` get a Bounce. Recipients at `complaint.example.test` get a Complaint.
- Emits `email.message.state_changed`, `email.unsubscribed`, `email.complaint`.
- Do not track opens or clicks.

## Tests
- State machines.
- Suppression and unsubscribe are honoured at send time.
- Quotas and rates.
- Duplicate, forged and wrong-topic SNS messages.
- One-click unsubscribe.
- A sanitization corpus.
- Idempotent send, and no duplicate after an ambiguous error.
- Auto-pause.
- Cross-tenant isolation and permissions.
