# Ordering agentic system on Google Cloud - one environment per state/project.
#
#   GKE Autopilot (private nodes, Dataplane V2, Workload Identity, Secret Manager + GCS FUSE CSI add-ons)
#   Cloud SQL for PostgreSQL 16 + pgvector (private IP, TLS-only, PITR, optional HA)
#   Memorystore for Redis 7 (AUTH + in-transit TLS, optional HA)
#   Artifact Registry, Secret Manager, Cloud Storage (RAG docs, AgentOps exports)
#   External HTTPS LB edge: static IP, Cloud Armor, TLS policy (the LB is created by the GKE Ingress)
#   GitHub Actions -> GCP via Workload Identity Federation (no service-account keys anywhere)

data "google_project" "this" {
  project_id = var.project_id
}

locals {
  name      = "ordering-${var.environment}"
  namespace = "ordering"
  # Workload Identity principal for a Kubernetes ServiceAccount (no Google service account needed).
  wi_member = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/${local.namespace}/sa"
}

resource "google_project_service" "apis" {
  for_each = toset([
    "artifactregistry.googleapis.com",
    "certificatemanager.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "containeranalysis.googleapis.com",
    "containerscanning.googleapis.com",
    "dns.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "redis.googleapis.com",
    "secretmanager.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
    "sts.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}
