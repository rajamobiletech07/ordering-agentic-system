# GKE Autopilot: Google manages nodes, and security defaults (Workload Identity, Shielded Nodes,
# Dataplane V2 NetworkPolicy enforcement, seccomp) are always on. Billing is per pod request.

# Least-privilege node identity (instead of the Compute Engine default SA, which usually holds
# Editor). Workloads never use it: each pod authenticates as its own KSA via Workload Identity.
resource "google_service_account" "nodes" {
  account_id   = "gke-nodes"
  display_name = "GKE Autopilot nodes (${var.environment})"
}

resource "google_project_iam_member" "nodes" {
  for_each = toset([
    "roles/artifactregistry.reader",
    "roles/autoscaling.metricsWriter",
    "roles/logging.logWriter",
    "roles/monitoring.metricWriter",
    "roles/monitoring.viewer",
    "roles/stackdriver.resourceMetadata.writer",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.nodes.member
}

resource "google_container_cluster" "this" {
  name     = local.name
  location = var.region # regional control plane; pods spread across zones

  enable_autopilot    = true
  deletion_protection = var.deletion_protection
  network             = google_compute_network.vpc.id
  subnetwork          = google_compute_subnetwork.gke.id

  cluster_autoscaling {
    auto_provisioning_defaults {
      service_account = google_service_account.nodes.email
      oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    }
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }

  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = false
  }

  # CI (GitHub-hosted runners with changing IPs) reaches the control plane through the DNS-based
  # endpoint, which Google IAM authorizes (container.clusters.connect), not an IP allowlist.
  control_plane_endpoints_config {
    dns_endpoint_config {
      allow_external_traffic = true
    }
  }

  master_authorized_networks_config {
    gcp_public_cidrs_access_enabled = false
    dynamic "cidr_blocks" {
      for_each = var.control_plane_authorized_cidrs
      content {
        cidr_block   = cidr_blocks.value.cidr
        display_name = cidr_blocks.value.name
      }
    }
  }

  release_channel {
    channel = var.environment == "production" ? "STABLE" : "REGULAR"
  }

  # Secret Manager CSI add-on: SecretProviderClass -> files (see deploy/k8s/base/secretproviderclasses.yaml)
  secret_manager_config {
    enabled = true
  }

  addons_config {
    gcs_fuse_csi_driver_config {
      enabled = true
    }
  }

  security_posture_config {
    mode               = "BASIC"
    vulnerability_mode = "VULNERABILITY_BASIC"
  }

  # Only images from this project's Artifact Registry may run (policy below). To also require
  # signed attestations, add an attestor to the policy (see the GCP deployment guide).
  binary_authorization {
    evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE"
  }

  maintenance_policy {
    recurring_window {
      start_time = "2026-01-01T02:00:00Z"
      end_time   = "2026-01-01T06:00:00Z"
      recurrence = "FREQ=WEEKLY;BYDAY=SA,SU"
    }
  }

  depends_on = [google_project_service.apis]
}

# Binary Authorization: only images from this project's Artifact Registry run. That covers the
# app image plus the Docker Hub / GHCR mirrors in registry.tf, so the OTel collector and LiteLLM are
# pulled through remote repositories that are scanned and not rate-limited. Google-maintained
# system images (e.g. the injected GCS FUSE sidecar) are allowed by the global policy.
resource "google_binary_authorization_policy" "this" {
  global_policy_evaluation_mode = "ENABLE"

  admission_whitelist_patterns {
    name_pattern = "${var.region}-docker.pkg.dev/${var.project_id}/**"
  }
  admission_whitelist_patterns {
    name_pattern = "gke.gcr.io/**"
  }

  default_admission_rule {
    evaluation_mode  = "ALWAYS_DENY"
    enforcement_mode = "ENFORCED_BLOCK_AND_AUDIT_LOG"
  }

  depends_on = [google_project_service.apis]
}
