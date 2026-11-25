# Brief: call-service, voice-service

**Paths you own:** `services/call_service`, `services/voice_service`, `services/fake_providers/fake_providers/routers/{twilio,deepgram,elevenlabs}.py`.
**Databases / app roles:** `call` / `call_app`, `voice` / `voice_app`.
**build.md:** §3.1 G, H; §5.4; §6; §11; §14 Phase 4; ADR-0003.

## call-service
- Tables:
  - `calls`: direction, state, contact, agent, campaign ids, numbers, `provider_call_id` (unique), scheduling and timing, end reason, `cost_micros`, opted_out, version.
  - `call_attempts`
  - `provider_events`: unique on (provider, event id).
  - `inbound_numbers`
  - `number_directory`: platform-scoped, ids only, e164 → tenant. Webhooks use it to find the tenant. It follows the tenant_service `membership_directory` pattern.
  - `call_outcomes`
- State machine: queued, scheduled, initiating, ringing, connected, in_progress, completed, failed, canceled, opted_out.
  - Enforce the legal transitions with optimistic locking.
  - Ignore duplicate and out-of-order callbacks safely.
- Internal API exactly as in conventions §5.
- Dispatch task (`calls.dispatch`). Claim the call, then apply the pre-dial gates. Record a reason for each one that blocks:
  1. Tenant `effective_calls_enabled`.
  2. Campaign `dispatch-state`.
  3. Calling window in the **contact's timezone**. Outside it, reschedule.
  4. Country allowlist.
  5. crm contactability.
  6. Tenant concurrency slot and daily cap.

  Then create the Twilio call with an idempotency token.
- After an ambiguous timeout or a 5xx:
  1. The call stays `initiating`.
  2. A reconcile task looks the call up at the provider.
  3. Only if the provider has no such call is it retried.

  There must never be two live calls for one call row.
- Webhooks:
  - Verify the signature with the tenant's Twilio token from `CredentialResolver`.
  - Resolve the tenant from our own rows or from `number_directory`.
  - Dedupe.
  - Process on `calls.provider_callbacks`.

  | Path | Behaviour |
  |---|---|
  | `/webhooks/twilio/status` | status updates |
  | `/webhooks/twilio/answer` | TwiML `<Connect><Stream>` to `/media/twilio`, with a short-lived signed stream token |
  | `/webhooks/twilio/voice` | inbound. Look up the number, create the call, return the same TwiML. Reject politely when the number is unknown or calls are disabled |

- Twilio adapter: real is REST through httpx. Fake is the simulator.
- Public API:
  - `GET /api/v1/calls` (`calls:read`), `GET /api/v1/calls/{id}`, `GET /api/v1/calls/live`
  - `POST /api/v1/calls/{id}/hangup` (`calls:manage`, audited)
  - Inbound numbers CRUD (`calls:manage`, audited)
  - `POST /api/v1/calls/test` (IK)
- Emits `call.state_changed`, `call.completed`, `call.opted_out`, and UI events.
- Consumes `campaign.paused`, `campaign.canceled` and `campaign.kill_switch`, and cancels queued calls.
- Periodic jobs: reconciliation, and a sweeper.

## voice-service
- `WS /media/twilio` speaks the Twilio Media Streams protocol. Authenticate the stream token. No stream may be unauthenticated.
- Pipeline:
  1. μ-law 8 kHz audio goes to STT. Deepgram is the real provider, plus a fake.
  2. The turn manager does endpointing and **barge-in**. On barge-in, send `clear` to Twilio and cancel the TTS and the in-flight model stream.
  3. The ai-gateway stream gets the turn (`purpose: voice_turn`). Use the runtime-config from ai-agent, and `context {call_id, contact_id}`. Agent turns are trusted. The caller's transcript is untrusted.
  4. Sentence-chunked TTS: ElevenLabs `ulaw_8000` real, plus a fake.
  5. Media frames go back to Twilio.
- Call start: play the AI disclosure and the recording-consent prompt, according to the tenant settings.
- Timeouts: no speech, max duration, provider failure. On failure, say a fallback line and end the call gracefully.
- Opt-out phrases:
  1. Confirm to the caller.
  2. End the call.
  3. Mark the contact opted out, through call-service and crm.
- Handoff: call-service `transfer`, plus the `voice.handoff.requested` event.
- Persistence: `voice_sessions`, `transcript_segments`, `recordings`, `handoffs`.
- On call end:
  1. Write the transcript JSON to the `transcripts` bucket.
  2. Emit `voice.session.ended`.
  3. Enqueue `voice.postprocess`, which calls ai-agent `/internal/v1/calls/postprocess`.
- Public API (`calls:recordings:read`, audited):
  - `GET /api/v1/voice/calls/{call_id}/transcript`
  - `GET /api/v1/voice/calls/{call_id}/recording` (a presigned URL)
- A retention job follows the tenant settings.

## Fake routers
- `twilio.py`:
  - REST calls with idempotency.
  - Scripted call progress that posts signed webhooks.
  - A scripted caller over the media WebSocket: a greeting, a booking request, and optionally an injection or an opt-out phrase.
  - An endpoint that triggers an inbound call.
- `deepgram.py` and `elevenlabs.py`.
- Sign with `FAKE_PROVIDER_SECRET`.

## Tests
- State machine.
- Forged, duplicate and out-of-order webhooks.
- Ambiguous timeout: the call is reconciled and never duplicated.
- Pre-dial gates.
- The inbound tenant cannot be spoofed.
- A stream token is required.
- Barge-in.
- Opt-out.
- Transcript and recording permissions, and cross-tenant isolation.
- One fully simulated call with the fakes.
