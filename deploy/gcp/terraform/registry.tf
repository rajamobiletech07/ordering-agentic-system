# Application images. Tags are immutable, and CI deploys by digest.
resource "google_artifact_registry_repository" "app" {
  repository_id = "ordering"
  location      = var.region
  format        = "DOCKER"
  description   = "Ordering agentic system images (built and signed by GitHub Actions)"

  docker_config {
    immutable_tags = true
  }

  cleanup_policy_dry_run = false
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 30
    }
  }
  cleanup_policies {
    id     = "delete-old-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "1209600s" # 14 days
    }
  }

  depends_on = [google_project_service.apis]
}

# Pull-through mirrors for third-party images (OTel collector, LiteLLM). They're scanned, avoid
# Docker Hub rate limits, and keep Binary Authorization to a single allowed registry.
resource "google_artifact_registry_repository" "dockerhub" {
  repository_id = "dockerhub"
  location      = var.region
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  description   = "Docker Hub mirror"
  remote_repository_config {
    docker_repository {
      public_repository = "DOCKER_HUB"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "ghcr" {
  repository_id = "ghcr"
  location      = var.region
  format        = "DOCKER"
  mode          = "REMOTE_REPOSITORY"
  description   = "GitHub Container Registry mirror"
  remote_repository_config {
    docker_repository {
      custom_repository {
        uri = "https://ghcr.io"
      }
    }
  }
  depends_on = [google_project_service.apis]
}
