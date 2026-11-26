# Metrics (kube-prometheus-stack + Grafana), logs (Loki), traces (Tempo) and the OpenTelemetry collector
# that services export OTLP to (build.md §10). Collector config mirrors infra/otel/collector.yml,
# including the attribute scrubbing processor.
resource "helm_release" "kube_prometheus_stack" {
  name       = "kube-prometheus-stack"
  namespace  = local.ns.observability
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = var.chart_versions.kube_prometheus_stack
  wait       = true
  timeout    = 900

  values = [
    file("${path.module}/values/kube-prometheus-stack.yaml"),
    yamlencode({
      prometheus = {
        prometheusSpec = {
          retention = var.prometheus_retention
        }
      }
      grafana = {
        admin = { existingSecret = var.grafana_admin_secret, userKey = "admin-user", passwordKey = "admin-password" }
      }
    }),
  ]

  depends_on = [kubernetes_namespace_v1.this]
}

resource "helm_release" "loki" {
  name       = "loki"
  namespace  = local.ns.observability
  repository = "https://grafana.github.io/helm-charts"
  chart      = "loki"
  version    = var.chart_versions.loki
  wait       = true
  timeout    = 900

  values = [file("${path.module}/values/loki.yaml")]

  depends_on = [kubernetes_namespace_v1.this]
}

resource "helm_release" "tempo" {
  name       = "tempo"
  namespace  = local.ns.observability
  repository = "https://grafana.github.io/helm-charts"
  chart      = "tempo"
  version    = var.chart_versions.tempo
  wait       = true

  values = [file("${path.module}/values/tempo.yaml")]

  depends_on = [kubernetes_namespace_v1.this]
}

resource "helm_release" "otel_collector" {
  name       = "otel-collector"
  namespace  = local.ns.observability
  repository = "https://open-telemetry.github.io/opentelemetry-helm-charts"
  chart      = "opentelemetry-collector"
  version    = var.chart_versions.opentelemetry_collector
  wait       = true

  values = [file("${path.module}/values/otel-collector.yaml")]

  depends_on = [helm_release.tempo, helm_release.loki, helm_release.kube_prometheus_stack]
}
