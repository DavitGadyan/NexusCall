#!/bin/sh
# DEVELOPMENT ONLY Vault bootstrap (ADR-0009). Refuses to run outside ENV=development.
# - enables AppRole + Transit, writes one least-privilege policy per service
# - issues AppRole credentials into the shared /approle volume (tmpfs-like, never committed)
# - seeds *fake* platform provider secrets so local fakes can authenticate
set -eu
[ "${ENV:-}" = "development" ] || { echo "refusing: ENV must be development"; exit 1; }
: "${VAULT_ADDR:?}"; : "${VAULT_TOKEN:?}"
E="${NEXUS_ENV_PATH:-development}"

until vault status >/dev/null 2>&1; do sleep 1; done

vault auth list | grep -q '^approle/' || vault auth enable approle
vault secrets list | grep -q '^transit/' || vault secrets enable transit
vault secrets list | grep -q '^secret/' || vault secrets enable -path=secret kv-v2
vault write -f transit/keys/bff-session >/dev/null
vault write -f transit/keys/webhook-signing >/dev/null
vault audit list 2>/dev/null | grep -q '^file/' || vault audit enable file file_path=stdout >/dev/null || true

# Policy template: read own platform secrets + listed tenant provider paths.
write_policy() {
  name="$1"; shift
  {
    echo "path \"secret/data/${E}/platform/${name}/*\" { capabilities = [\"read\"] }"
    for p in "$@"; do echo "$p"; done
    echo 'path "auth/token/renew-self" { capabilities = ["update"] }'
    echo 'path "auth/token/lookup-self" { capabilities = ["read"] }'
  } > "/tmp/${name}.hcl"
  vault policy write "$name" "/tmp/${name}.hcl" >/dev/null
  vault write "auth/approle/role/${name}" token_policies="$name" token_ttl=1h token_max_ttl=4h \
    secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
  mkdir -p "/approle/${name}"
  vault read -field=role_id "auth/approle/role/${name}/role-id" > "/approle/${name}/role_id"
  vault write -f -field=secret_id "auth/approle/role/${name}/secret-id" > "/approle/${name}/secret_id"
  chmod 0444 "/approle/${name}/role_id" "/approle/${name}/secret_id"
}

T="secret/data/${E}/tenants/+"
M="secret/metadata/${E}/tenants/+"
write_policy tenant-service 'path "transit/encrypt/bff-session" { capabilities = ["update"] }' \
  "path \"secret/data/${E}/service-accounts/*\" { capabilities = [\"create\", \"update\"] }" \
  "path \"secret/metadata/${E}/service-accounts/*\" { capabilities = [\"read\", \"delete\"] }"
write_policy crm-service
write_policy campaign-service
write_policy call-service "path \"${T}/twilio/*\" { capabilities = [\"read\"] }"
write_policy voice-service "path \"${T}/twilio/*\" { capabilities = [\"read\"] }" \
  "path \"${T}/deepgram/*\" { capabilities = [\"read\"] }" "path \"${T}/elevenlabs/*\" { capabilities = [\"read\"] }"
write_policy ai-agent-service
write_policy ai-gateway "path \"${T}/anthropic/*\" { capabilities = [\"read\"] }"
write_policy mcp-registry
write_policy tool-execution "path \"${T}/google_calendar/*\" { capabilities = [\"read\"] }" \
  "path \"${T}/hubspot/*\" { capabilities = [\"read\"] }"
write_policy booking-service "path \"${T}/google_calendar/*\" { capabilities = [\"read\"] }"
write_policy email-service "path \"${T}/ses/*\" { capabilities = [\"read\"] }"
write_policy email-inbox-service
# integration-service is the ONLY writer of tenant provider secrets.
write_policy integration-service \
  "path \"${T}/*\" { capabilities = [\"create\", \"update\", \"read\", \"delete\"] }" \
  "path \"secret/data/${E}/tenants/+/+/*\" { capabilities = [\"create\", \"update\", \"read\", \"delete\"] }" \
  "path \"secret/metadata/${E}/tenants/+/+/*\" { capabilities = [\"read\", \"delete\", \"list\"] }" \
  "path \"${M}/*\" { capabilities = [\"read\", \"list\"] }" \
  'path "transit/sign/webhook-signing" { capabilities = ["update"] }'
write_policy notification-service
write_policy analytics-service
write_policy audit-service
write_policy web-bff 'path "transit/encrypt/bff-session" { capabilities = ["update"] }' \
  'path "transit/decrypt/bff-session" { capabilities = ["update"] }'

# Fake platform credentials for the local fake providers (clearly labelled; not real secrets).
vault kv put "secret/${E}/platform/ai-gateway/anthropic" api_key="fake-anthropic-key-dev-only" >/dev/null
vault kv put "secret/${E}/platform/voice-service/deepgram" api_key="fake-deepgram-key-dev-only" >/dev/null
vault kv put "secret/${E}/platform/voice-service/elevenlabs" api_key="fake-elevenlabs-key-dev-only" >/dev/null
vault kv put "secret/${E}/platform/email-service/ses" region="us-east-1" access_key_id="FAKEDEVONLY" \
  secret_access_key="fake-ses-secret-dev-only" >/dev/null
vault kv put "secret/${E}/platform/notification-service/ses" region="us-east-1" access_key_id="FAKEDEVONLY" \
  secret_access_key="fake-ses-secret-dev-only" >/dev/null
# Broker operations identity used by the platform console (queue view, audited dead-letter replay).
vault kv put "secret/${E}/platform/tenant-service/rabbitmq-ops" username="nexus-ops" \
  password="${NEXUS_BROKER_OPS_PASSWORD:?NEXUS_BROKER_OPS_PASSWORD must be set (dev only)}" >/dev/null
echo "vault bootstrap complete"
