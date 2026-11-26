terraform {
  required_version = ">= 1.6.0"

  required_providers {
    vault = {
      source  = "hashicorp/vault"
      version = "~> 5.14"
    }
  }

  # Remote state: see ../addons/backend.tf.example (use a separate key, e.g. vault-config/production).
  # This module's state contains the bootstrap admin passwords of the database and broker connections
  # (sensitive variables). Rotate them right after the first apply (docs/runbooks/secret-rotation.md):
  # `vault write -f database/rotate-root/nexus-pg` makes the stored PostgreSQL password useless.
}

# Authenticate with VAULT_ADDR, VAULT_CACERT and a short-lived VAULT_TOKEN of an operator (never the root
# token after bootstrap). Nothing is hard-coded here.
provider "vault" {}
