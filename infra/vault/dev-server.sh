#!/bin/sh
# DEVELOPMENT ONLY: start a persistent single-node Vault, initialise it on first start, unseal it on
# every start. The unseal key and the generated root token are kept in the vault-data volume, next to
# the data they protect. That is acceptable on a laptop and nowhere else (ADR-0009).
set -eu
[ "${ENV:-development}" = "development" ] || { echo "refusing: ENV must be development"; exit 1; }
: "${VAULT_DEV_ROOT_TOKEN:?VAULT_DEV_ROOT_TOKEN must be set (make env)}"

INIT_FILE=/vault/data/dev-init.txt
READY=/tmp/vault-ready
rm -f "$READY"
mkdir -p /vault/data/storage
export VAULT_ADDR=http://127.0.0.1:8200

vault server -config=/vault/config/server.hcl &
PID=$!
trap 'kill -TERM "$PID" 2>/dev/null' TERM INT

# `vault status` exits 0 when unsealed, 2 when sealed, 1 when the server is not answering yet.
rc=1
while [ "$rc" -eq 1 ]; do
  sleep 1
  rc=0; vault status >/dev/null 2>&1 || rc=$?
done

if vault status 2>/dev/null | grep -Eq '^Initialized[[:space:]]+false'; then
  echo "vault: first start, initialising (1 key share, development only)"
  ( umask 077; vault operator init -key-shares=1 -key-threshold=1 > "$INIT_FILE" )
fi
[ -s "$INIT_FILE" ] || { echo "vault: storage is initialised but $INIT_FILE is missing; run 'make clean'"; exit 1; }

UNSEAL_KEY=$(awk -F': ' '/^Unseal Key 1:/ {print $2}' "$INIT_FILE")
ROOT_TOKEN=$(awk -F': ' '/^Initial Root Token:/ {print $2}' "$INIT_FILE")
vault operator unseal "$UNSEAL_KEY" >/dev/null
echo "vault: unsealed"

# The rest of the stack (bootstrap, ops tooling) uses the token id from .env.
if ! VAULT_TOKEN="$ROOT_TOKEN" vault token lookup "$VAULT_DEV_ROOT_TOKEN" >/dev/null 2>&1; then
  VAULT_TOKEN="$ROOT_TOKEN" vault token create -id="$VAULT_DEV_ROOT_TOKEN" -policy=root -orphan \
    -display-name=dev-admin >/dev/null
  echo "vault: dev admin token created"
fi

touch "$READY"  # healthcheck: unsealed AND the admin token exists
wait "$PID"
