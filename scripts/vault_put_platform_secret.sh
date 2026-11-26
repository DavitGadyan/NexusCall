#!/usr/bin/env bash
# Write one platform default provider secret to the local Vault (development only).
#
#   scripts/vault_put_platform_secret.sh ai-gateway/anthropic api_key
#   printf %s "$KEY" | scripts/vault_put_platform_secret.sh voice-service/deepgram api_key
#
# The value is read from stdin (hidden prompt when interactive). It never appears in argv, shell
# history or logs. The UI (Platform admin -> Secrets) writes to the same path.
set -euo pipefail
cd "$(dirname "$0")/.."

path="${1:?usage: $0 <service>/<provider> <field>}"
field="${2:?usage: $0 <service>/<provider> <field>}"
[[ "$path" =~ ^[a-z-]+/[a-z0-9-]+$ ]] || { echo "invalid path: $path" >&2; exit 2; }
[[ "$field" =~ ^[a-z_]+$ ]] || { echo "invalid field: $field" >&2; exit 2; }

if [ -t 0 ]; then
  read -r -s -p "Value for ${path} ${field}: " value; echo
else
  IFS= read -r value || true
fi
[ -n "$value" ] || { echo "empty value, nothing written" >&2; exit 1; }

# Inside the container: read the value once, then patch the existing secret or create it.
printf %s "$value" | docker compose exec -T -e P="$path" -e F="$field" vault sh -c '
  export VAULT_ADDR=http://127.0.0.1:8200 VAULT_TOKEN="$VAULT_DEV_ROOT_TOKEN"
  v=$(cat); target="${ENV:-development}/platform/$P"
  if vault kv get -mount=secret "$target" >/dev/null 2>&1; then
    printf %s "$v" | vault kv patch -mount=secret "$target" "$F=-" >/dev/null
  else
    printf %s "$v" | vault kv put -mount=secret "$target" "$F=-" >/dev/null
  fi'
echo "stored secret/${ENV:-development}/platform/${path} (${field})"
