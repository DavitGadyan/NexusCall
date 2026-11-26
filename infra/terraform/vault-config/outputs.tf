output "kubernetes_roles" {
  description = "Vault Kubernetes-auth roles and the ServiceAccount each is bound to."
  value       = merge(local.service_accounts, { "nexus-migrations" = "nexus-migrations" })
}

output "policies" {
  description = "Rendered policy paths per role (no secrets)."
  value       = { for k, v in local.policies : k => [for r in v : r.path] }
}
