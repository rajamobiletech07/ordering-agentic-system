# --- Workload Identity: Kubernetes ServiceAccount -> exactly the secrets it mounts ------------------
# Mirrors deploy/k8s/base/secretproviderclasses.yaml. Keep the two in sync.
locals {
  ksa_secrets = {
    "api-gateway"         = ["ordering-redis-url", "ordering-redis-ca", "ordering-jwt-secret"]
    "agent-service"       = ["ordering-database-url", "ordering-redis-url", "ordering-redis-ca", "ordering-jwt-secret", "ordering-service-jwt-secret"]
    "llm-gateway"         = ["ordering-database-url", "ordering-redis-url", "ordering-redis-ca", "ordering-service-jwt-secret", "ordering-anthropic-api-key", "ordering-openai-api-key", "ordering-google-api-key", "ordering-litellm-api-key"]
    "mcp-server"          = ["ordering-database-url", "ordering-redis-url", "ordering-redis-ca", "ordering-service-jwt-secret"]
    "notification-worker" = ["ordering-database-url"]
    "ordering-jobs"       = ["ordering-database-admin-url", "ordering-database-url", "ordering-app-db-password", "ordering-service-jwt-secret", "ordering-jwt-secret"]
    "litellm"             = ["ordering-litellm-api-key", "ordering-anthropic-api-key", "ordering-openai-api-key", "ordering-google-api-key"]
  }
  ksa_secret_pairs = merge([
    for ksa, secrets in local.ksa_secrets : { for s in secrets : "${ksa}/${s}" => { ksa = ksa, secret = s } }
  ]...)
  all_secret_ids = merge(
    { for k, v in google_secret_manager_secret.generated : k => v.id },
    { for k, v in google_secret_manager_secret.external : k => v.id },
  )
}

resource "google_secret_manager_secret_iam_member" "ksa" {
  for_each  = local.ksa_secret_pairs
  secret_id = local.all_secret_ids[each.value.secret]
  role      = "roles/secretmanager.secretAccessor"
  member    = "${local.wi_member}/${each.value.ksa}"
}

# Telemetry: the gateway collector writes traces and metrics.
resource "google_project_iam_member" "otel_collector" {
  for_each = toset(["roles/cloudtrace.agent", "roles/monitoring.metricWriter"])
  project  = var.project_id
  role     = each.value
  member   = "${local.wi_member}/otel-collector"
}

# Jobs: read RAG documents, write AgentOps feedback exports.
resource "google_storage_bucket_iam_member" "jobs_rag_read" {
  bucket = google_storage_bucket.rag_docs.name
  role   = "roles/storage.objectViewer"
  member = "${local.wi_member}/ordering-jobs"
}

resource "google_storage_bucket_iam_member" "jobs_agentops_write" {
  bucket = google_storage_bucket.agentops.name
  role   = "roles/storage.objectUser"
  member = "${local.wi_member}/ordering-jobs"
}

# --- GitHub Actions -> GCP (Workload Identity Federation, no keys) ----------------------------------
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub OIDC"
  attribute_mapping = {
    "google.subject"        = "assertion.sub"
    "attribute.repository"  = "assertion.repository"
    "attribute.ref"         = "assertion.ref"
    "attribute.environment" = "assertion.environment"
  }
  # Only this repository can exchange tokens at all.
  attribute_condition = "assertion.repository == '${var.github_repository}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# Deployer: push images, deploy to this cluster. Only jobs running in the matching GitHub
# Environment can impersonate it, so production deploys inherit that environment's required
# reviewers and branch/tag rules.
resource "google_service_account" "deployer" {
  account_id   = "gh-deployer"
  display_name = "GitHub Actions deployer (${var.environment})"
}

resource "google_service_account_iam_member" "deployer_wif" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.environment/${var.environment}"
}

resource "google_project_iam_member" "deployer" {
  for_each = toset([
    "roles/container.developer", # namespaced deploys + DNS-endpoint access (container.clusters.connect)
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.deployer.member
}

resource "google_artifact_registry_repository_iam_member" "deployer_push" {
  repository = google_artifact_registry_repository.app.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

resource "google_artifact_registry_repository_iam_member" "readers" {
  for_each   = toset(var.artifact_readers)
  repository = google_artifact_registry_repository.app.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = each.value
}

resource "google_storage_bucket_iam_member" "deployer_rag_docs" {
  count  = var.ci_manages_rag_docs ? 1 : 0
  bucket = google_storage_bucket.rag_docs.name
  role   = "roles/storage.objectAdmin"
  member = google_service_account.deployer.member
}

# Terraform runner for .github/workflows/terraform.yml (plan/apply), in GitHub Environment
# "<env>-infra". Curated admin roles instead of roles/owner.
resource "google_service_account" "terraform" {
  account_id   = "gh-terraform"
  display_name = "GitHub Actions Terraform (${var.environment})"
}

resource "google_service_account_iam_member" "terraform_wif" {
  service_account_id = google_service_account.terraform.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.environment/${var.environment}-infra"
}

resource "google_project_iam_member" "terraform" {
  for_each = toset([
    "roles/artifactregistry.admin",
    "roles/binaryauthorization.policyEditor",
    "roles/cloudsql.admin",
    "roles/compute.networkAdmin",
    "roles/compute.securityAdmin",
    "roles/container.admin",
    "roles/dns.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.workloadIdentityPoolAdmin",
    "roles/redis.admin",
    "roles/resourcemanager.projectIamAdmin",
    "roles/secretmanager.admin",
    "roles/servicenetworking.networksAdmin",
    "roles/serviceusage.serviceUsageAdmin",
    "roles/storage.admin",
  ])
  project = var.project_id
  role    = each.value
  member  = google_service_account.terraform.member
}

# actAs only on the node identity it attaches to the cluster, not project-wide.
resource "google_service_account_iam_member" "terraform_act_as_nodes" {
  service_account_id = google_service_account.nodes.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.terraform.member
}
