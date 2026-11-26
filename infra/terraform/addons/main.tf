# Cluster add-ons for NexusCall (ADR-0004). Install order and the two-step first apply are described in
# docs/DEPLOYMENT.md. No secret values pass through this module: passwords are created out of band as
# Kubernetes Secrets or in Vault, and referenced by name.

locals {
  topology = jsondecode(file("${path.module}/../generated/topology.json"))

  # Pod Security per namespace. Application and data planes are `restricted`; components that need
  # host access (node exporter, log collection) or root (ClamAV) are isolated in their own namespaces.
  namespaces = {
    edge          = { name = "nexus-edge", enforce = "restricted" }
    data          = { name = "nexus-data", enforce = "baseline" }
    vault         = { name = "nexus-vault", enforce = "baseline" }
    identity      = { name = "nexus-identity", enforce = "restricted" }
    observability = { name = "nexus-observability", enforce = "privileged" }
    platform      = { name = "nexus-platform", enforce = "baseline" }
    cert_manager  = { name = "cert-manager", enforce = "restricted" }
    cnpg          = { name = "cnpg-system", enforce = "restricted" }
    rabbitmq      = { name = "rabbitmq-system", enforce = "restricted" }
    app           = { name = var.app_namespace, enforce = "restricted" }
  }
  ns = { for k, v in local.namespaces : k => v.name }

  storage_class = var.storage_class == "" ? null : var.storage_class
}

resource "kubernetes_namespace_v1" "this" {
  for_each = local.namespaces

  metadata {
    name = each.value.name
    labels = {
      "pod-security.kubernetes.io/enforce"         = each.value.enforce
      "pod-security.kubernetes.io/enforce-version" = "latest"
      "pod-security.kubernetes.io/audit"           = "restricted"
      "pod-security.kubernetes.io/warn"            = "restricted"
      "app.kubernetes.io/part-of"                  = "nexuscall"
      "nexuscall.io/environment"                   = var.environment
    }
  }
}

# Default-deny ingress for the platform namespaces; each add-on opens what it needs below.
# (The application namespace gets default-deny ingress AND egress from the Helm chart.)
resource "kubernetes_network_policy_v1" "data_ingress" {
  metadata {
    name      = "nexus-data-ingress"
    namespace = local.ns.data
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
    ingress {
      # Same namespace (replication, operators' sidecars, bootstrap jobs).
      from {
        pod_selector {}
      }
    }
    ingress {
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.app }
        }
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.vault }
        }
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.identity }
        }
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.observability }
        }
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.cnpg }
        }
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.rabbitmq }
        }
      }
    }
  }
}

resource "kubernetes_network_policy_v1" "vault_ingress" {
  metadata {
    name      = "nexus-vault-ingress"
    namespace = local.ns.vault
  }
  spec {
    pod_selector {}
    policy_types = ["Ingress"]
    ingress {
      from {
        pod_selector {}
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.app }
        }
      }
      from {
        namespace_selector {
          match_labels = { "kubernetes.io/metadata.name" = local.ns.observability }
        }
      }
    }
    ingress {
      # Kubernetes API server -> injector webhook.
      ports {
        port     = 8080
        protocol = "TCP"
      }
    }
  }
}
