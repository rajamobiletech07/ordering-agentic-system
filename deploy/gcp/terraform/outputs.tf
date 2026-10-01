# Non-secret identifiers consumed by deploy/gcp/scripts/write-values.sh ->
# deploy/k8s/overlays/<env>/values.env (and from there by render.sh and the GitHub workflows).
output "values_env" {
  description = "Contents for deploy/k8s/overlays/<env>/values.env"
  value = {
    PROJECT_ID             = var.project_id
    REGION                 = var.region
    GKE_CLUSTER            = google_container_cluster.this.name
    IMAGE_REPO             = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}/ordering-app"
    APP_HOST               = var.app_host
    STATIC_IP_NAME         = google_compute_global_address.api.name
    SECURITY_POLICY        = google_compute_security_policy.edge.name
    SSL_POLICY             = google_compute_ssl_policy.tls.name
    PRIVATE_SERVICES_CIDR  = var.private_services_cidr
    RAG_BUCKET             = google_storage_bucket.rag_docs.name
    AGENTOPS_BUCKET        = google_storage_bucket.agentops.name
    WIF_PROVIDER           = google_iam_workload_identity_pool_provider.github.name
    DEPLOY_SERVICE_ACCOUNT = google_service_account.deployer.email
    TF_SERVICE_ACCOUNT     = google_service_account.terraform.email
  }
}

output "api_ip_address" {
  description = "Create a DNS A record: <app_host> -> this address (the managed certificate provisions after it resolves)."
  value       = google_compute_global_address.api.address
}

output "cloud_sql_instance" {
  value = google_sql_database_instance.this.connection_name
}

output "llm_key_secrets" {
  description = "Add a version to each before the first deploy (deploy/gcp/scripts/set-llm-keys.sh)."
  value       = local.external_secrets
}
