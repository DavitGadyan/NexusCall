# DEVELOPMENT ONLY: single-node Vault with file storage, so secrets entered in the UI survive restarts.
# Staging/production use the Helm-deployed Vault with integrated storage and K8s auth (docs/DEPLOYMENT.md).
storage "file" {
  path = "/vault/data/storage"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true # internal Docker network only; the host port is bound to 127.0.0.1
}

api_addr      = "http://vault:8200"
disable_mlock = true
ui            = true
