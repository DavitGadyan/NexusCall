# ---------------------------------------------------------------- cert-manager
resource "helm_release" "cert_manager" {
  name       = "cert-manager"
  namespace  = local.ns.cert_manager
  repository = "oci://quay.io/jetstack/charts"
  chart      = "cert-manager"
  version    = var.chart_versions.cert_manager
  wait       = true
  timeout    = 600

  values = [file("${path.module}/values/cert-manager.yaml")]

  depends_on = [kubernetes_namespace_v1.this]
}

# ---------------------------------------------------------------- Traefik (ADR-0010)
resource "helm_release" "traefik" {
  name       = "traefik"
  namespace  = local.ns.edge
  repository = "https://traefik.github.io/charts"
  chart      = "traefik"
  version    = var.chart_versions.traefik
  wait       = true
  timeout    = 600

  values = [
    file("${path.module}/values/traefik.yaml"),
    yamlencode({
      providers = {
        kubernetesCRD = {
          # Only these namespaces may define routes; cross-namespace references stay off.
          namespaces          = [local.ns.app, local.ns.identity]
          allowCrossNamespace = false
        }
      }
      # The web BFF forwards the client IP; trust it only from inside the cluster.
      ports = {
        internal = { forwardedHeaders = { trustedIPs = [var.pod_cidr] } }
      }
      service = {
        spec = {
          loadBalancerSourceRanges = var.load_balancer_source_ranges
        }
      }
    }),
  ]

  depends_on = [kubernetes_namespace_v1.this]
}

# ---------------------------------------------------------------- issuers, internal CA, Vault TLS, Keycloak route
resource "helm_release" "cluster_config" {
  name      = "nexus-cluster-config"
  namespace = local.ns.platform
  chart     = "${path.module}/../charts/cluster-config"
  wait      = true

  values = [yamlencode({
    certManagerNamespace = local.ns.cert_manager
    acme                 = { email = var.acme_email, ingressClass = "traefik" }
    vault                = { namespace = local.ns.vault, serviceName = "vault", replicas = var.vault_replicas }
    keycloak = {
      namespace   = local.ns.identity
      host        = var.identity_host
      serviceName = "keycloak-keycloakx-http"
      servicePort = 80
      issuer      = var.acme_email == "" ? "nexus-internal-ca" : "letsencrypt-production"
      entryPoint  = "websecure"
    }
  })]

  depends_on = [helm_release.cert_manager, helm_release.traefik]
}
