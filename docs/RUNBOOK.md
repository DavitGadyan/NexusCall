# NexusCall — Runbook

## Local stack
- **Start:** `make up PROFILE="core"`, then add profiles as needed. Status: `python3 scripts/stack_status.py`.
- **One-off jobs** run automatically and must exit 0: `vault-bootstrap`, `rabbitmq-bootstrap`, `objectstore-init`, `migrate`.
- **Reset local data:** `make clean`. This deletes the Postgres, RabbitMQ and object-store volumes.
- **Host ports** use the 127.0.0.1:47xxx range, so they don't clash with other local stacks. Override them with `NEXUS_PORT_*` in `.env`.

### Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `migrate` exits 1 with "database system is in recovery mode" | Postgres restarted mid-migration. Re-run `make migrate`. Check `docker compose logs postgres`. |
| Keycloak unhealthy, "Unable to resolve auth flow binding override" | Realm JSON is out of sync. Run `make generate`, then `make clean` (realm import only runs on an empty DB). |
| `rabbitmq-bootstrap` cannot authenticate | The admin user comes from `RABBITMQ_DEFAULT_*` and only exists on a fresh `rabbitdata` volume. |
| `buildx ... permission denied` on `~/.docker/buildx` | Files there are root-owned from an earlier `sudo docker`. Fix with `sudo chown -R $(whoami) ~/.docker`, or point `DOCKER_CONFIG` at a copy. |

## Migrations
- Migrations run as a **single controlled job** under each service's `<db>_owner` role (`scripts/migrate_all.py`). Never run them from application replicas.
- RLS policies live only in migrations (`nexus_core.rls.apply_tenant_rls`). The admin UI never edits them.
- In production: a Helm pre-upgrade Job, with Vault-issued owner credentials (`MIGRATION_DATABASE_URL_<DB>`).

## Secrets (Vault)
- **Layout:** `secret/<env>/tenants/<tenant>/<provider>/<connection>`, `secret/<env>/platform/<service>/<name>`.
- **Writers:** only integration-service writes tenant secrets. Each service reads only its own paths (`infra/vault/bootstrap.sh`).
- **Rotation (planned):** create the new credential → validate it through the connection test → switch over → verify health → revoke the old one. Every step is audited.
- **Suspected compromise:**
  1. Revoke the credential in the provider console and in Vault: `vault kv metadata delete`.
  2. Disable the affected connection in the UI. Dependent jobs fail as permanent.
  3. Revoke the affected sessions in Keycloak.
  4. Record the incident in the audit log.

## Incident basics
- **Kill switches:** tenant-level and platform-level stop switches for calls, email and AI. In the UI: Platform → Settings, or Tenant admin → Communication policy.
- **Dead-letter queues:** inspect and replay only through Platform → Queues & DLQ. Replays need `platform:queues:replay` and are audited.

## Backups and DR (production)
- **Postgres:** CloudNativePG continuous WAL archiving to S3-compatible storage. Test PITR restores quarterly (Phase 8 drill).
- **Vault:** Raft snapshots every hour, kept for 30 days. Store unseal and recovery keys offline under split custody.
- **Object storage:** bucket versioning, plus replication to a second site.
