# Brief: email-inbox-service (inbound email, AI drafts, human approval)

**Paths you own:** `services/email_inbox_service`, `services/fake_providers/fake_providers/routers/ses_inbound.py`.
**Database / app role:** `inbox` / `inbox_app`.
**build.md:** §3.1 L; §4.2 and "Draft review requirements"; §24.6; §14 Phase 6.

## Tables
- `mailboxes`, plus a platform-scoped, ids-only `mailbox_directory` (address → tenant).
- `threads`
- `messages`: `rfc_message_id` is tenant-unique. Also `in_reply_to`, `references`, text, sanitized HTML, the raw object key.
- `attachments`: scan status, quarantined.
- `drafts`
- `draft_versions`: **append-only**. Body, author ai|user, model, provider, prompt version, citations, quality.
- `draft_actions`: **append-only**.
- `send_commands`: unique on (draft_id, version).
- `provider_events`: unique.

## Inbound
- `/webhooks/ses/inbound`:
  1. Verify with SNS. Fake mode uses the fake verifier settings.
  2. The notification points to the raw MIME in the `raw-mail` bucket.
  3. Enqueue `emails.inbound_process`.
- Processing:
  1. Fetch the MIME, with a size cap.
  2. Parse it with the stdlib `email` package.
  3. Resolve the mailbox and tenant **from the envelope recipient through `mailbox_directory`**. Never use headers the sender controls.
  4. Dedupe on the SNS id and the `Message-ID`.
  5. Thread by `In-Reply-To` and `References`. Use email-service `messages/lookup` for replies to campaigns.
  6. Find the contact through crm `lookup`.
  7. Store the text and the HTML, sanitized with bleach.
  8. Attachments:
     - Apply type and size limits.
     - Scan with ClamAV over TCP INSTREAM (`CLAMAV_HOST`).
     - An infected file is quarantined and never downloadable.
     - If the scanner is unavailable, the status is `skipped` in development and `error` in production. `error` blocks the download.
  9. Store auto-replies and loops, but do not draft for them.
  10. Emit `email.inbound.received`.
  11. Enqueue `emails.ai_draft` when the mailbox has auto-draft on and the sender is not suppressed.

## AI draft
- Build untrusted context blocks, then call ai-agent `/internal/v1/drafting/runs`.
- Store draft version 1 with its provenance.
- Emit `email.reply_draft.generated`.
- Sensitive intents are flagged `escalated`: complaint, legal, privacy request, unsubscribe.
- If drafting fails, the thread still appears for a human.

## Review API
- `GET /api/v1/email/inbox/threads` (`emails:inbox:read`), and thread detail.
- Attachment download: a presigned URL, only when the file is clean. Audited.
- `GET /api/v1/email/drafts` (`emails:drafts:review`), and draft detail. The detail shows the original message, the AI draft clearly marked, citations, versions and actions.
- `PUT /drafts/{id}`: an edit creates a new version (`expected_version`).
- `regenerate`, `reject`, `assign`, `escalate`.
- **`POST /drafts/{id}/approve`** (`emails:drafts:approve`, step-up, Idempotency-Key, body `{version}`):
  1. Check the permission and the tenant ownership at that moment.
  2. The version must be the current one.
  3. Freeze it.
  4. Create the unique `send_command`.
  5. Audit, write `email.reply_draft.approved` to the outbox, and enqueue `emails.send_approved`.
- `POST /drafts/{id}/send` only retries a failed send of an **already approved** version. It accepts `{version}` and nothing else.

## Send task
1. Load the frozen approved version.
2. Check again:
   - The approver still holds `emails:drafts:approve`, through tenant-service `/internal/v1/authz/resolve`.
   - The tenant's `effective_emails_enabled`.
   - The recipient is not suppressed.
3. Call email-service `/internal/v1/send`:
   - category `reply`
   - from = the mailbox address
   - `In-Reply-To` and `References` set
   - idempotency key `draft:{id}:v{n}`
4. Record the outbound message and emit `email.reply.sent`.

**There is no code path that sends an AI draft without a persisted human approval. Do not implement auto-send.**

## Other
- Mailboxes API (`emails:campaigns:manage`).
- Fake router `ses_inbound.py`: `POST /ses-inbound/simulate` builds the MIME, stores it in `raw-mail` and posts a signed SNS notification to `INBOUND_WEBHOOK_URL`.
- A retention job.

## Tests
- **An unapproved draft cannot be sent**, by any path: the endpoint, a forged status, a stale version, or the task run directly.
- Approval needs the permission, step-up and the current version.
- A double approve or double send produces one send.
- If the approver loses the permission before the send, nothing is sent.
- An edit after approval needs a new approval.
- An injection corpus in body, HTML and attachment names still produces only a draft, with no side effect.
- Spoofed headers cannot change the tenant.
- Duplicate inbound events.
- An infected attachment is quarantined.
- Cross-tenant isolation.
- The append-only history cannot be changed.
