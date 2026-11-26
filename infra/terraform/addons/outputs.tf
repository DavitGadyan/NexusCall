output "namespaces" {
  description = "Namespaces created for NexusCall and its add-ons."
  value       = local.ns
}

output "endpoints" {
  description = "In-cluster endpoints to use in the NexusCall Helm values (global.*)."
  value = {
    database_host    = "nexus-pg-rw.${local.ns.data}.svc"
    redis_host       = "redis-ha-haproxy.${local.ns.data}.svc"
    rabbitmq_host    = "nexus-rabbitmq.${local.ns.data}.svc"
    vault_addr       = "https://vault.${local.ns.vault}.svc:8200"
    keycloak_url     = "http://keycloak-keycloakx-http.${local.ns.identity}.svc"
    otel_endpoint    = "http://otel-collector-opentelemetry-collector.${local.ns.observability}.svc:4318"
    object_store     = var.object_storage_mode == "seaweedfs" ? "http://seaweedfs-s3.${local.ns.data}.svc:8333" : var.external_s3_endpoint
    traefik_internal = "http://traefik-internal.${local.ns.edge}.svc:8081"
    clamav_host      = "clamav.${local.ns.platform}.svc"
  }
}

output "task_queues" {
  description = "Celery task queues; each has a <queue>.dlq on nexus.dlx (from generated/topology.json)."
  value       = local.topology.task_queues
}
