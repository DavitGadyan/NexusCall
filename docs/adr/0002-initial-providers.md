# ADR-0002: Initial provider selection

- Status: Accepted (user decision)
- Date: 2026-11-25

## Context
build.md §2 and §16 ask for one working default per integration category, behind adapters.

## Decision
| Category | Initial real adapter | Local fake |
|---|---|---|
| Telephony | Twilio Programmable Voice + Media Streams | `fake_providers/twilio` (REST + signed webhooks + media WS) |
| Email outbound | Amazon SES v2 (API, configuration sets → SNS events) | `fake_providers/ses` + Mailpit SMTP sink |
| Email inbound | SES receipt rules → S3 + SNS notification | `fake_providers/ses` inbound injector |
| LLM | Anthropic Messages API (`claude-haiku-5-5` voice, `claude-sonnet-5-5` drafting, `claude-opus-5-5` evals) | Deterministic scripted LLM |
| Embeddings | Configurable embedding adapter (default: Voyage via gateway), pgvector storage | Hash-based deterministic vectors |
| STT | Deepgram streaming | Scripted transcript streamer |
| TTS | ElevenLabs streaming (`ulaw_8000`) | Tone/silence generator |
| Calendar | Google Calendar (OAuth) | In-memory calendar |
| CRM | HubSpot (Phase 7) | In-memory CRM |

Adapters implement the `packages/provider_contracts` Protocols, and the same contract test suite runs against both the fake and the real adapter.

**Fake guard:** `PROVIDER_MODE=fake` is honoured only when `ENV in {development, test}`. Any service whose fake adapter is resolved while `ENV=production` raises at startup. CI also fails any production Helm values file that sets `PROVIDER_MODE=fake`.

## Consequences
- AWS is needed for SES, even though compute runs self-hosted (see ADR-0004). SES credentials live in Vault.
- Amazon Connect, Postmark, OpenAI and Google can be added later as further adapters.
