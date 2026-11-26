# Keycloak (ADR-0006) on the CloudNativePG cluster (database `keycloak`, role managed by CNPG from the
# Secret `keycloak-db-credentials`, which must also exist in nexus-identity with keys username/password).
# The realm itself is configuration, not infrastructure: see docs/DEPLOYMENT.md "Identity".
resource "helm_release" "keycloak" {
  name       = "keycloak"
  namespace  = local.ns.identity
  repository = "https://codecentric.github.io/helm-charts"
  chart      = "keycloakx"
  version    = var.chart_versions.keycloakx
  wait       = true
  timeout    = 900

  values = [
    file("${path.module}/values/keycloak.yaml"),
    yamlencode({
      replicas = var.keycloak_replicas
      image = {
        repository = split(":", var.keycloak_image)[0]
        tag        = split(":", var.keycloak_image)[1]
      }
      extraEnv = yamlencode([
        { name = "KC_HOSTNAME", value = "https://${var.identity_host}" },
        { name = "KC_HOSTNAME_BACKCHANNEL_DYNAMIC", value = "true" },
        { name = "KC_PROXY_HEADERS", value = "xforwarded" },
        { name = "KC_HTTP_ENABLED", value = "true" },
        { name = "KC_HEALTH_ENABLED", value = "true" },
        { name = "KC_METRICS_ENABLED", value = "true" },
        { name = "KC_LOG_CONSOLE_OUTPUT", value = "json" },
        { name = "KC_DB_URL_PROPERTIES", value = "?sslmode=require" },
      ])
    }),
  ]

  depends_on = [helm_release.platform_data, helm_release.cluster_config]
}
