variable "environment" {
  description = "staging or production: the {env} segment of every KV path (ADR-0009)."
  type        = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "app_namespace" {
  description = "Namespace whose ServiceAccounts may log in (the NexusCall Helm release)."
  type        = string
  default     = "nexuscall"
}

variable "kubernetes_host" {
  description = "Kubernetes API URL as seen from the Vault pods."
  type        = string
  default     = "https://kubernetes.default.svc"
}

variable "token_ttl_seconds" {
  description = "Service token TTL (mirrors the development AppRoles: 1 h, max 4 h)."
  type        = number
  default     = 3600
}

variable "token_max_ttl_seconds" {
  type    = number
  default = 14400
}

# ---------------------------------------------------------------- database secrets engine
variable "postgres_host" {
  type    = string
  default = "nexus-pg-rw.nexus-data.svc"
}

variable "postgres_vault_admin_password" {
  description = "Password of the CNPG-managed vault_admin role (Secret nexus-pg-vault-admin). Pass via TF_VAR_; rotate-root afterwards."
  type        = string
  sensitive   = true
}

variable "db_credential_ttl_seconds" {
  description = "Default lease of dynamic database users."
  type        = number
  default     = 86400
}

variable "db_credential_max_ttl_seconds" {
  description = "Maximum lease. Pods read DATABASE_URL at start, so they must be restarted within this window (docs/DEPLOYMENT.md)."
  type        = number
  default     = 604800
}

variable "owner_rotation_period_seconds" {
  description = "Rotation period of the static <db>_owner passwords used by migrations."
  type        = number
  default     = 86400
}

# ---------------------------------------------------------------- RabbitMQ secrets engine
variable "rabbitmq_management_url" {
  type    = string
  default = "http://nexus-rabbitmq.nexus-data.svc:15672"
}

variable "rabbitmq_vault_admin_password" {
  description = "Password of the operator-generated vault-admin broker user (Secret vault-admin-user-credentials). Pass via TF_VAR_."
  type        = string
  sensitive   = true
}

variable "broker_credential_ttl_seconds" {
  type    = number
  default = 86400
}

variable "broker_credential_max_ttl_seconds" {
  type    = number
  default = 604800
}

variable "audit_file_path" {
  description = "File audit device path on the Vault audit volume."
  type        = string
  default     = "/vault/audit/vault_audit.log"
}
