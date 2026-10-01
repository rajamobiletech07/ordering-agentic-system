terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.30"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial configuration: `terraform init -backend-config=envs/<env>.gcs.tfbackend`.
  # The state bucket is created once by deploy/gcp/scripts/bootstrap.sh. State contains generated
  # passwords, so the bucket is private, versioned and restricted to the Terraform identity.
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
  default_labels = {
    app         = "ordering"
    environment = var.environment
    managed-by  = "terraform"
  }
}
