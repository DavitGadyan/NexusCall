#!/usr/bin/env bash
# DEVELOPMENT ONLY. Creates one database per service with three roles each (ADR-0007):
#   <db>_owner     owns the schema; used only by migrations
#   <db>_app       runtime role: NOSUPERUSER NOBYPASSRLS, DML grants per table from migrations
#   <db>_inspector read-only catalog access for the RLS coverage dashboard
# In production, Vault's database secrets engine issues short-lived credentials instead.
set -euo pipefail
: "${NEXUS_DB_DEV_PASSWORD:?NEXUS_DB_DEV_PASSWORD must be set (dev only)}"

DBS="tenant crm campaign call voice agent aigw mcpreg toolexec booking email inbox integration notify analytics audit"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
REVOKE ALL ON DATABASE postgres FROM PUBLIC;
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'keycloak') THEN
    CREATE ROLE keycloak LOGIN PASSWORD '${NEXUS_DB_DEV_PASSWORD}';
  END IF;
END \$\$;
SQL
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "CREATE DATABASE keycloak OWNER keycloak" || true

for db in $DBS; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${db}_owner') THEN
    CREATE ROLE ${db}_owner LOGIN PASSWORD '${NEXUS_DB_DEV_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${db}_app') THEN
    CREATE ROLE ${db}_app LOGIN PASSWORD '${NEXUS_DB_DEV_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
      CONNECTION LIMIT 60;
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${db}_inspector') THEN
    CREATE ROLE ${db}_inspector LOGIN PASSWORD '${NEXUS_DB_DEV_PASSWORD}' NOSUPERUSER NOBYPASSRLS CONNECTION LIMIT 3;
  END IF;
END \$\$;
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -c "CREATE DATABASE ${db} OWNER ${db}_owner" || true
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<SQL
REVOKE ALL ON DATABASE ${db} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${db} TO ${db}_app, ${db}_inspector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO ${db}_owner;
GRANT USAGE ON SCHEMA public TO ${db}_app, ${db}_inspector;
ALTER DEFAULT PRIVILEGES FOR ROLE ${db}_owner IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO ${db}_app;
-- Extensions that are not 'trusted' must be created by the bootstrap superuser.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS btree_gist;
SQL
done

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname agent -c "CREATE EXTENSION IF NOT EXISTS vector"
echo "nexus: databases and roles ready"
