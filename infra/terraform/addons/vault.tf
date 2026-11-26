# HashiCorp Vault: HA with integrated Raft storage, TLS listener (cert from the internal CA), file audit
# device on its own volume, and the Agent Injector used by every NexusCall pod (ADR-0009).
# Policies, auth roles and secret engines are configured by ../vault-config after init/unseal.
resource "helm_release" "vault" {
  name       = "vault"
  namespace  = local.ns.vault
  repository = "https://helm.releases.hashicorp.com"
  chart      = "vault"
  version    = var.chart_versions.vault
  wait       = false # pods stay unready until initialised and unsealed (docs/DEPLOYMENT.md)
  timeout    = 900

  values = [
    file("${path.module}/values/vault.yaml"),
    yamlencode({
      server = {
        ha = {
          enabled  = true
          replicas = var.vault_replicas
          raft = {
            enabled   = true
            setNodeId = true
            config = templatefile("${path.module}/values/vault-raft.hcl.tftpl", {
              replicas    = var.vault_replicas
              namespace   = local.ns.vault
              seal_stanza = var.vault_seal_stanza
            })
          }
        }
        dataStorage  = { storageClass = local.storage_class }
        auditStorage = { enabled = true, size = "20Gi", storageClass = local.storage_class }
      }
    }),
  ]

  depends_on = [helm_release.cluster_config]
}
