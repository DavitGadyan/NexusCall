terraform {
  required_version = ">= 1.6.0"

  required_providers {
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.5"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.6"
    }
  }

  # Remote state: copy backend.tf.example to backend.tf and fill it in. State holds no secrets from this
  # module (no passwords are passed in), but treat it as sensitive anyway: encrypted, access-controlled.
}
