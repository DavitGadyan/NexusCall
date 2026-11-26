# ---------------------------------------------------------------- CloudNativePG (PostgreSQL 16 + pgvector)
resource "helm_release" "cnpg" {
  name       = "cnpg"
  namespace  = local.ns.cnpg
  repository = "https://cloudnative-pg.github.io/charts"
  chart      = "cloudnative-pg"
  version    = var.chart_versions.cloudnative_pg
  wait       = true

  values = [yamlencode({
    replicaCount = 2
    monitoring   = { podMonitorEnabled = true }
  })]

  depends_on = [kubernetes_namespace_v1.this]
}

# Barman Cloud plugin: WAL archiving and base backups to S3-compatible storage (PITR).
resource "helm_release" "cnpg_barman_cloud" {
  name       = "barman-cloud"
  namespace  = local.ns.cnpg
  repository = "https://cloudnative-pg.github.io/charts"
  chart      = "plugin-barman-cloud"
  version    = var.chart_versions.plugin_barman_cloud
  wait       = true

  depends_on = [helm_release.cnpg, helm_release.cert_manager]
}

# ---------------------------------------------------------------- RabbitMQ operators (official release manifests)
data "http" "rabbitmq_operator" {
  for_each = var.rabbitmq_operator_manifests
  url      = each.value.url

  lifecycle {
    postcondition {
      condition     = self.status_code == 200 && sha256(self.response_body) == each.value.sha256
      error_message = "Downloaded ${each.key} manifest does not match the pinned SHA-256; refusing to apply it."
    }
  }
}

resource "helm_release" "rabbitmq_operators" {
  name      = "rabbitmq-operators"
  namespace = local.ns.rabbitmq
  chart     = "${path.module}/../charts/raw-manifests"
  wait      = true
  timeout   = 600

  values = [yamlencode({
    manifests = [
      data.http.rabbitmq_operator["cluster_operator"].response_body,
      data.http.rabbitmq_operator["topology_operator"].response_body,
    ]
  })]

  # The topology operator's webhook certificate is issued by cert-manager.
  depends_on = [helm_release.cert_manager, kubernetes_namespace_v1.this]
}

# ---------------------------------------------------------------- data plane CRs (PostgreSQL cluster, broker topology)
resource "helm_release" "platform_data" {
  name      = "nexus-platform-data"
  namespace = local.ns.data
  chart     = "${path.module}/../charts/platform-data"
  wait      = true
  timeout   = 1800

  values = [yamlencode({
    postgres = {
      name       = "nexus-pg"
      instances  = var.postgres_instances
      imageName  = var.postgres_image
      storage    = { size = var.postgres_storage_size, storageClass = var.storage_class }
      walStorage = { size = "20Gi", storageClass = var.storage_class }
      databases  = local.topology.databases
      backup = {
        enabled             = true
        destinationPath     = var.postgres_backup_destination
        endpointURL         = var.postgres_backup_endpoint
        s3CredentialsSecret = "nexus-pg-backup-s3"
        retentionPolicy     = var.postgres_backup_retention
        schedule            = "0 0 2 * * *"
      }
    }
    rabbitmq = {
      name               = "nexus-rabbitmq"
      replicas           = var.rabbitmq_replicas
      storage            = { size = "20Gi", storageClass = var.storage_class }
      vhost              = "nexus"
      exchanges          = [local.topology.event_exchange, local.topology.task_exchange, local.topology.dead_letter_exchange]
      deadLetterExchange = local.topology.dead_letter_exchange.name
      deadLetterQueues   = local.topology.dead_letter_queues
      dlqPolicy = {
        name       = local.topology.dlq_policy.name
        pattern    = local.topology.dlq_policy.pattern
        definition = local.topology.dlq_policy.definition
      }
    }
  })]

  depends_on = [helm_release.cnpg_barman_cloud, helm_release.rabbitmq_operators]
}

# ---------------------------------------------------------------- Redis (HA with Sentinel behind HAProxy)
# Services use one REDIS_URL, so HAProxy fronts the current master. The password lives in the Secret
# `redis-auth` (key `password`, created out of band) and in Vault at secret/<env>/platform/shared/redis.
resource "helm_release" "redis" {
  name       = "redis-ha"
  namespace  = local.ns.data
  repository = "https://dandydeveloper.github.io/charts"
  chart      = "redis-ha"
  version    = var.chart_versions.redis_ha
  wait       = true

  values = [file("${path.module}/values/redis-ha.yaml")]

  depends_on = [kubernetes_namespace_v1.this]
}

# ---------------------------------------------------------------- object storage (ADR-0013)
resource "helm_release" "seaweedfs" {
  count      = var.object_storage_mode == "seaweedfs" ? 1 : 0
  name       = "seaweedfs"
  namespace  = local.ns.data
  repository = "https://seaweedfs.github.io/seaweedfs/helm"
  chart      = "seaweedfs"
  version    = var.chart_versions.seaweedfs
  wait       = true
  timeout    = 900

  values = [file("${path.module}/values/seaweedfs.yaml")]

  depends_on = [kubernetes_namespace_v1.this]
}

# ---------------------------------------------------------------- ClamAV (attachment scanning, email-inbox-service)
resource "kubernetes_deployment_v1" "clamav" {
  metadata {
    name      = "clamav"
    namespace = local.ns.platform
    labels    = { app = "clamav" }
  }
  spec {
    replicas = 2
    selector {
      match_labels = { app = "clamav" }
    }
    template {
      metadata {
        labels = { app = "clamav" }
      }
      spec {
        automount_service_account_token = false
        container {
          name  = "clamd"
          image = var.clamav_image
          port {
            container_port = 3310
          }
          resources {
            requests = { cpu = "250m", memory = "1536Mi" }
            limits   = { memory = "2Gi" }
          }
          readiness_probe {
            tcp_socket {
              port = 3310
            }
            initial_delay_seconds = 60
            period_seconds        = 10
          }
          security_context {
            allow_privilege_escalation = false
            capabilities {
              drop = ["ALL"]
              add  = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE"]
            }
            seccomp_profile {
              type = "RuntimeDefault"
            }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_namespace_v1.this]
}

resource "kubernetes_service_v1" "clamav" {
  metadata {
    name      = "clamav"
    namespace = local.ns.platform
  }
  spec {
    selector = { app = "clamav" }
    port {
      port        = 3310
      target_port = 3310
    }
  }
}

resource "kubernetes_network_policy_v1" "clamav" {
  metadata {
    name      = "clamav"
    namespace = local.ns.platform
  }
  spec {
    pod_selector {
      match_labels = { app = "clamav" }
    }
    policy_types = ["Ingress"]
    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.app }
        }
        pod_selector {
          match_labels = { "app.kubernetes.io/component" = "email-inbox-service" }
        }
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.app }
        }
        pod_selector {
          match_labels = { "app.kubernetes.io/component" = "worker-email-inbound" }
        }
      }
      ports {
        port     = 3310
        protocol = "TCP"
      }
    }
  }
}
