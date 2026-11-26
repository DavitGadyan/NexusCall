# Runbook: signing-key and session-key rotation

| Key | Used for | Overlap | Cadence |
|---|---|---|---|
| Keycloak realm signing keys (RS256) | Access, ID and refresh tokens; services verify them through JWKS | yes (multiple active keys, `kid`) | every 90 days |
| BFF session key `NEXUS_SESSION_KEY` (`platform/web-bff/session`) | AES-256-GCM encryption of BFF sessions in Redis | **no**: one key, all sessions end | every 180 days, or on suspicion |
| Media-stream token secret `STREAM_TOKEN_SECRET` | call-service signs `<Stream>` tokens (TTL 120 s); voice-service verifies them | **no**: one shared value in **two** Vault paths | every 90 days |
| Transit `webhook-signing` | Outbound webhook signatures (integration-service) | yes (transit keeps old versions for verification) | every 90 days |
| Transit `bff-session` | Wrapping the BFF session key material (ADR-0009) | yes (transit versions) | every 180 days |
| Provider webhook secrets (Twilio auth token, SNS certificates) | Inbound webhook verification | Twilio: secondary token. SNS: AWS rotates its certificates | per provider |

## 1. Keycloak realm signing keys (overlapping)
1. Keycloak admin (port-forward, MFA): Realm `nexus` → Realm settings → Keys → Providers → add `rsa-generated` with a **higher priority** than the current one. New tokens are signed with the new `kid`. The old key stays active for verification.
2. Wait at least the refresh-token lifetime (the session max, 10 h by default), so no valid token still references the old key. Access tokens last 5 minutes.
3. Verify that services accept new tokens: `k6 run tests/load/smoke.js` with a fresh token, and no rise in 401s on the dashboard. Services re-fetch JWKS for an unknown `kid`.
4. Set the old provider to passive, then delete it after another refresh lifetime.
5. **Emergency (key compromised):**
   1. Delete the old key at once.
   2. Sign out all realm sessions (Realm → Sessions → Sign out all).
   3. Accept that every user must sign in again.
   4. Restart the web BFF so cached tokens are dropped.

## 2. BFF session key (non-overlapping)
Impact: every user is signed out once. No data is lost. Schedule a low-traffic window and announce it.
1. Generate a key: `openssl rand -hex 32 > /dev/shm/k`. Then `vault kv put secret/production/platform/web-bff/session key=@/dev/shm/k` and `shred -u /dev/shm/k`.
2. `kubectl -n nexuscall rollout restart deploy/web`. The agent renders the new key, and old sessions fail to decrypt, which sends users to log in.
3. Optionally flush the session keys in Redis DB 1 to free memory. They are unreadable now anyway.
4. **Rollback:** `vault kv rollback -version=<previous> secret/production/platform/web-bff/session`, then restart again.

## 3. Media-stream token secret (non-overlapping; two paths, one value)
call-service and voice-service must hold **the same** value. Rotate them together. A mismatch makes every new call's media stream fail authentication, so the caller hears silence and the call ends with a fallback.
1. Choose a low-traffic window. Optionally pause outbound dialing with the tenant or platform calls [kill switch](kill-switches.md). Live calls are not affected: their tokens were checked at stream start.
2. Write the same new value to both paths:
   ```bash
   openssl rand -hex 32 > /dev/shm/st
   vault kv put secret/production/platform/call-service/stream-token secret=@/dev/shm/st
   vault kv put secret/production/platform/voice-service/stream-token secret=@/dev/shm/st
   shred -u /dev/shm/st
   ```
3. Restart both, together: `kubectl -n nexuscall rollout restart deploy/voice-service deploy/call-service deploy/worker-calls`. For about the restart time plus 120 s, tokens issued by an old call-service pod are rejected by new voice-service pods. Those calls fail and campaign retries pick them up.
4. Verify that one test call (`POST /api/v1/calls/test`) connects media and produces transcript segments.
5. Lift the kill switch.

## 4. Transit keys (overlapping)
```bash
vault write -f transit/keys/webhook-signing/rotate
vault write transit/keys/webhook-signing/config min_decryption_version=<n-1>   # after consumers have moved
```
Tenant webhook receivers verify our HMAC signature with their endpoint secret (`nexus_core.webhooks`). Rotating an **endpoint secret** is a tenant action: Integrations → Webhooks → Rotate. The UI shows the new secret once, and both secrets sign during the overlap configured there.

## 5. Twilio auth token (inbound signature + REST)
1. In the Twilio console, create a **secondary** auth token.
2. Write it to the tenant connection through the UI (Rotate → Test).
3. Promote it to primary at Twilio. From that moment Twilio signs webhooks with the new token. Our resolver cache is 60 s, so expect up to a minute of signature failures. Twilio retries status callbacks.
4. Verify that the status webhooks are accepted (dashboard: webhook 403 rate back to 0).

## 6. Records
For each rotation, record:
- the key name and path (never the value)
- the old and new version
- the start and end time
- the impact observed
- the operator and the reviewer

The Vault audit device records the KV writes. Restarts show in the Kubernetes events.
