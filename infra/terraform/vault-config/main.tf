# Vault configuration for Kubernetes (ADR-0009): the production counterpart of infra/vault/bootstrap.sh.
#   * Policies: exactly the per-service paths of bootstrap.sh (parsed into ../generated/topology.json by
#     infra/helm/generate.py, so they cannot drift), plus the Kubernetes-only paths each pod needs:
#     its dynamic database and broker credentials and the shared Redis password.
#   * Auth: Kubernetes auth method; role = service name, bound to the ServiceAccount of the same name in
#     the application namespace, audience `vault` (the chart's projected token).
#   * Engines: KV v2, transit (same keys as development), database (dynamic <db>_app/_inspector/_platform
#     members, static <db>_owner for migrations), RabbitMQ (per-service users, permissions from
#     scripts/bootstrap_rabbitmq.py).
#   * Audit device: file, on the Vault audit volume.

locals {
  topology = jsondecode(file("${path.module}/../generated/topology.json"))
  env      = var.environment

  # service name -> database name, e.g. "crm-service" -> "crm"
  databases = local.topology.databases

  bootstrap_rules = {
    for name, rules in local.topology.vault_policies : name => [
      for r in rules : { path = replace(r.path, "{env}", local.env), capabilities = r.capabilities }
    ]
  }

  shared_redis = { path = "secret/data/${local.env}/platform/shared/redis", capabilities = ["read"] }

  kubernetes_rules = merge(
    {
      for svc, db in local.databases : svc => [
        { path = "database/creds/${db}-app", capabilities = ["read"] },
        { path = "database/creds/${db}-inspector", capabilities = ["read"] },
        { path = "database/creds/${db}-platform", capabilities = ["read"] },
        { path = "rabbitmq/creds/${svc}", capabilities = ["read"] },
        local.shared_redis,
      ]
    },
    { "web-bff" = [local.shared_redis] },
  )

  policies = {
    for name, rules in local.bootstrap_rules : name => concat(rules, lookup(local.kubernetes_rules, name, []))
  }

  # Vault role -> ServiceAccount (web runs as SA `web` with the `web-bff` policy, as in development).
  service_accounts = merge({ for svc, _ in local.databases : svc => svc }, { "web-bff" = "web" })
}

# ---------------------------------------------------------------- audit
resource "vault_audit" "file" {
  type = "file"
  options = {
    file_path = var.audit_file_path
    log_raw   = "false" # secret values are HMAC'd in the audit log
  }
}

# ---------------------------------------------------------------- KV v2 and transit
resource "vault_mount" "kv" {
  path        = "secret"
  type        = "kv"
  options     = { version = "2" }
  description = "NexusCall static secrets: secret/<env>/platform/... and secret/<env>/tenants/..."
}

resource "vault_mount" "transit" {
  path        = "transit"
  type        = "transit"
  description = "BFF session key wrapping and webhook signing (same keys as infra/vault/bootstrap.sh)"
}

resource "vault_transit_secret_backend_key" "keys" {
  for_each         = toset(["bff-session", "webhook-signing"])
  backend          = vault_mount.transit.path
  name             = each.key
  deletion_allowed = false
  exportable       = false
}

# ---------------------------------------------------------------- password policy for generated credentials
# Alphanumeric only: the values are embedded in connection URLs and shell-quoted env files.
resource "vault_password_policy" "alnum" {
  name   = "nexus-alnum-32"
  policy = <<-EOT
    length = 32
    rule "charset" {
      charset   = "abcdefghijklmnopqrstuvwxyz"
      min-chars = 2
    }
    rule "charset" {
      charset   = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
      min-chars = 2
    }
    rule "charset" {
      charset   = "0123456789"
      min-chars = 2
    }
  EOT
}

# ---------------------------------------------------------------- policies
resource "vault_policy" "service" {
  for_each = local.policies
  name     = each.key
  policy = join("\n", [
    for r in each.value : "path \"${r.path}\" { capabilities = ${jsonencode(r.capabilities)} }"
  ])
}

resource "vault_policy" "migrations" {
  name = "nexus-migrations"
  policy = join("\n", concat(
    [for svc, db in local.databases : "path \"database/static-creds/${db}-owner\" { capabilities = [\"read\"] }"],
    [
      "path \"auth/token/renew-self\" { capabilities = [\"update\"] }",
      "path \"auth/token/lookup-self\" { capabilities = [\"read\"] }",
    ],
  ))
}

# ---------------------------------------------------------------- Kubernetes auth
resource "vault_auth_backend" "kubernetes" {
  type        = "kubernetes"
  path        = "kubernetes"
  description = "NexusCall workloads (Vault Agent Injector)"
}

resource "vault_kubernetes_auth_backend_config" "this" {
  backend         = vault_auth_backend.kubernetes.path
  kubernetes_host = var.kubernetes_host
  # Vault runs in-cluster: it uses its own pod's CA and token to call TokenReview.
  disable_local_ca_jwt = false
}

resource "vault_kubernetes_auth_backend_role" "service" {
  for_each                         = local.service_accounts
  backend                          = vault_auth_backend.kubernetes.path
  role_name                        = each.key
  bound_service_account_names      = [each.value]
  bound_service_account_namespaces = [var.app_namespace]
  audience                         = "vault"
  token_policies                   = [vault_policy.service[each.key].name]
  token_ttl                        = var.token_ttl_seconds
  token_max_ttl                    = var.token_max_ttl_seconds
  token_type                       = "service"
}

resource "vault_kubernetes_auth_backend_role" "migrations" {
  backend                          = vault_auth_backend.kubernetes.path
  role_name                        = "nexus-migrations"
  bound_service_account_names      = ["nexus-migrations"]
  bound_service_account_namespaces = [var.app_namespace]
  audience                         = "vault"
  token_policies                   = [vault_policy.migrations.name]
  token_ttl                        = 1800
  token_max_ttl                    = 3600
}
