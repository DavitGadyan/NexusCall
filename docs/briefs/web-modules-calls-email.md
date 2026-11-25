# Brief: web pages for calls, bookings, AI approvals, email campaigns and the inbox

**Route folders you own:** `app/calls/**`, `app/bookings/**`, `app/approvals/**`, `app/campaigns/email/**`, `app/inbox/**`. Under the tenant admin shell: `admin/email/**`, `admin/numbers/**`.
**Components:** `components/calls/**`, `components/bookings/**`, `components/email/**`, `components/inbox/**`.
**Contracts:** `call_service.json`, `voice_service.json`, `booking_service.json`, `tool_execution.json`, `email_service.json`, `email_inbox_service.json`.

## Pages
- **Live calls**: a real-time board, driven by `useLiveEvents` with polling as the fallback. Hang up action.
- **Call history**: filters. Detail page with timeline, outcome and summary. Transcript and recording playback through the short-lived URL. Each access is audited, and the page says so.
- **Phone numbers** (admin): inbound numbers, with agent and hours.
- **Bookings**: calendar (week and day) and list. Create, reschedule, cancel. Resources and availability rules. Conflicts shown clearly.
- **AI approvals**: the queue of AI-proposed actions that wait for a person.
  - Show what the tool will do and for whom, and the context.
  - Approve needs step-up. Reject.
  - History.
- **Email senders and domains** (admin): DNS records to copy, verification status for SPF, DKIM and DMARC.
- **Templates**: an editor with a sanitized preview, versions, and test send.
- **Email campaigns**: builder (template, segment, sender, window, schedule). Schedule, pause, resume, cancel. A report with delivery states. Do not show open or click rates.
- **Inbox**: thread list with filters and assignment. Thread view with messages and attachments. Scan status is visible, and an infected file cannot be downloaded.
- **Approval queue and draft review**:
  - The original message next to the AI draft. The draft is clearly labelled as AI-written and not yet sent.
  - Cited knowledge sources.
  - Version history.
  - Edit, which creates a new version.
  - Regenerate, reject, assign, escalate.
  - **Approve and send** needs step-up and an explicit confirmation that shows the recipient and the exact text.

  There is no control anywhere that sends without approval.
