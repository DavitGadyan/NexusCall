# ---------------------------------------------------------------- database secrets engine (ADR-0007, ADR-0009)
resource "vault_mount" "database" {
  path                      = "database"
  type                      = "database"
  default_lease_ttl_seconds = var.db_credential_ttl_seconds
  max_lease_ttl_seconds     = var.db_credential_max_ttl_seconds
}

resource "vault_database_secret_backend_connection" "postgres" {
  backend         = vault_mount.database.path
  name            = "nexus-pg"
  password_policy = vault_password_policy.alnum.name
  allowed_roles = concat(
    flatten([for svc, db in local.databases : ["${db}-app", "${db}-inspector", "${db}-platform"]]),
    [for svc, db in local.databases : "${db}-owner"],
  )
  verify_connection = true

  postgresql {
    connection_url          = "postgresql://{{username}}:{{password}}@${var.postgres_host}:5432/postgres?sslmode=require"
    username                = "vault_admin"
    password                = var.postgres_vault_admin_password
    password_authentication = "scram-sha-256"
    max_open_connections    = 5
  }
}

locals {
  dynamic_db_roles = merge([
    for svc, db in local.databases : {
      "${db}-app"       = { group = "${db}_app", limit = 40 }
      "${db}-inspector" = { group = "${db}_inspector", limit = 3 }
      "${db}-platform"  = { group = "${db}_platform", limit = 10 }
    }
  ]...)
}

# Short-lived LOGIN members of the NOLOGIN group roles created by the platform-data bootstrap Job.
# They inherit the group's grants and RLS policies; never superuser, never BYPASSRLS, never owner.
resource "vault_database_secret_backend_role" "dynamic" {
  for_each    = local.dynamic_db_roles
  backend     = vault_mount.database.path
  name        = each.key
  db_name     = vault_database_secret_backend_connection.postgres.name
  default_ttl = var.db_credential_ttl_seconds
  max_ttl     = var.db_credential_max_ttl_seconds
  creation_statements = [
    "CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS INHERIT CONNECTION LIMIT ${each.value.limit} IN ROLE ${each.value.group};",
  ]
  renew_statements = [
    "ALTER ROLE \"{{name}}\" VALID UNTIL '{{expiration}}';",
  ]
  revocation_statements = [
    "REVOKE ${each.value.group} FROM \"{{name}}\";",
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{{name}}';",
    "DROP ROLE IF EXISTS \"{{name}}\";",
  ]
}

# Migrations run as the real schema owner, whose password Vault rotates.
resource "vault_database_secret_backend_static_role" "owner" {
  for_each        = local.databases
  backend         = vault_mount.database.path
  name            = "${each.value}-owner"
  db_name         = vault_database_secret_backend_connection.postgres.name
  username        = "${each.value}_owner"
  rotation_period = var.owner_rotation_period_seconds
}

# ---------------------------------------------------------------- RabbitMQ secrets engine (ADR-0011)
resource "vault_rabbitmq_secret_backend" "broker" {
  path                      = "rabbitmq"
  connection_uri            = var.rabbitmq_management_url
  username                  = "vault-admin"
  password                  = var.rabbitmq_vault_admin_password
  verify_connection         = true
  password_policy           = vault_password_policy.alnum.name
  default_lease_ttl_seconds = var.broker_credential_ttl_seconds
  max_lease_ttl_seconds     = var.broker_credential_max_ttl_seconds
}

# One role per service; vhost and topic permissions are the regexes of scripts/bootstrap_rabbitmq.py, so a
# service may publish only its own event types (THREAT_MODEL B6).
resource "vault_rabbitmq_secret_backend_role" "service" {
  for_each = local.topology.broker_roles
  backend  = vault_rabbitmq_secret_backend.broker.path
  name     = each.key
  tags     = ""

  vhost {
    host      = "nexus"
    configure = each.value.configure
    write     = each.value.write
    read      = each.value.read
  }

  vhost_topic {
    host = "nexus"
    vhost {
      topic = each.value.topic_exchange
      write = each.value.topic_write
      read  = each.value.topic_read
    }
  }
}
