# ---------------------------------------------------------------- cluster access
variable "kubeconfig_path" {
  description = "Path to the kubeconfig for the target cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context of the target cluster (explicit, so a wrong cluster is never used)."
  type        = string
}

variable "environment" {
  description = "staging or production. Used in namespaces' labels and Vault paths."
  type        = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

# ---------------------------------------------------------------- names
variable "app_namespace" {
  description = "Namespace of the NexusCall Helm release (Pod Security: restricted)."
  type        = string
  default     = "nexuscall"
}

variable "public_host" {
  description = "Public host of the application, e.g. app.example.com."
  type        = string
}

variable "identity_host" {
  description = "Public host of Keycloak, e.g. id.example.com."
  type        = string
}

variable "acme_email" {
  description = "ACME account e-mail for the Let's Encrypt cluster issuers (empty: no ACME issuers)."
  type        = string
  default     = ""
}

variable "pod_cidr" {
  description = "Cluster pod CIDR; Traefik trusts X-Forwarded-For on its internal entrypoint only from here."
  type        = string
  default     = "10.0.0.0/8"
}

variable "load_balancer_source_ranges" {
  description = "CIDRs allowed to reach the public load balancer (default: everyone)."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "storage_class" {
  description = "StorageClass for persistent volumes (empty: cluster default)."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------- chart versions (pinned; bump deliberately)
variable "chart_versions" {
  description = "Pinned Helm chart versions. Verify each against its repository before an upgrade."
  type        = map(string)
  default = {
    traefik                 = "42.4.3"
    cert_manager            = "v1.20.5"
    cloudnative_pg          = "0.29.2"
    plugin_barman_cloud     = "0.9.0"
    vault                   = "0.34.1"
    keycloakx               = "7.4.0"
    redis_ha                = "4.39.0"
    seaweedfs               = "4.55.0"
    kube_prometheus_stack   = "93.11.0"
    loki                    = "7.7.0"
    tempo                   = "1.24.4"
    opentelemetry_collector = "0.183.2"
  }
}

variable "rabbitmq_operator_manifests" {
  description = "RabbitMQ Cluster Operator and Messaging Topology Operator release manifests, verified by SHA-256 before use."
  type = map(object({
    url    = string
    sha256 = string
  }))
  default = {
    cluster_operator = {
      url    = "https://github.com/rabbitmq/cluster-operator/releases/download/v2.23.3/cluster-operator.yml"
      sha256 = "3ccd8a7c8a7f319ff21c444f3599829242f3cc1c18ae2dedea170cfcefeb8681"
    }
    topology_operator = {
      url    = "https://github.com/rabbitmq/messaging-topology-operator/releases/download/v1.20.6/messaging-topology-operator-with-certmanager.yaml"
      sha256 = "1c58de4f838fe49a156cd67b3c6f00ecb2782eef4f6fa9cf95f664fc19f4164f"
    }
  }
}

# ---------------------------------------------------------------- PostgreSQL (CloudNativePG)
variable "postgres_image" {
  description = "CloudNativePG PostgreSQL 16 image that includes pgvector. Pin by digest in production."
  type        = string
  default     = "ghcr.io/cloudnative-pg/postgresql:16.4-56-bookworm"
}

variable "postgres_instances" {
  type    = number
  default = 3
}

variable "postgres_storage_size" {
  type    = string
  default = "100Gi"
}

variable "postgres_backup_destination" {
  description = "S3-compatible destination for WAL archives and base backups, e.g. s3://nexus-pg-backups/production."
  type        = string
}

variable "postgres_backup_endpoint" {
  description = "Endpoint URL of a non-AWS S3-compatible backup store (empty for AWS S3)."
  type        = string
  default     = ""
}

variable "postgres_backup_retention" {
  type    = string
  default = "30d"
}

# ---------------------------------------------------------------- RabbitMQ
variable "rabbitmq_replicas" {
  type    = number
  default = 3
}

# ---------------------------------------------------------------- object storage
variable "object_storage_mode" {
  description = "seaweedfs (in-cluster, ADR-0013) or external (any S3-compatible service; credentials in Vault)."
  type        = string
  default     = "seaweedfs"
  validation {
    condition     = contains(["seaweedfs", "external"], var.object_storage_mode)
    error_message = "object_storage_mode must be seaweedfs or external."
  }
}

variable "external_s3_endpoint" {
  description = "Endpoint of the external S3-compatible store when object_storage_mode = external."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------- Vault
variable "vault_replicas" {
  type    = number
  default = 3
}

variable "vault_seal_stanza" {
  description = "Optional HCL seal stanza for auto-unseal (transit or a cloud KMS). Empty: Shamir, manual unseal."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------- Keycloak
variable "keycloak_replicas" {
  type    = number
  default = 2
}

variable "keycloak_image" {
  description = "Keycloak image (same major as development: 26.x)."
  type        = string
  default     = "quay.io/keycloak/keycloak:26.0.7"
}

# ---------------------------------------------------------------- observability
variable "grafana_admin_secret" {
  description = "Existing Secret in nexus-observability with keys admin-user / admin-password (created out of band)."
  type        = string
  default     = "grafana-admin"
}

variable "prometheus_retention" {
  type    = string
  default = "15d"
}

variable "clamav_image" {
  type    = string
  default = "clamav/clamav:1.4.7"
}
