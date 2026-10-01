# terraform -chdir=deploy/gcp/terraform init -backend-config=envs/staging.gcs.tfbackend
# terraform -chdir=deploy/gcp/terraform apply -var-file=envs/staging.tfvars
project_id        = "my-ordering-staging"
environment       = "staging"
region            = "europe-west2"
app_host          = "ordering-staging.example.com"
github_repository = "my-org/ordering-agentic-system"

db_tier              = "db-custom-1-3840"
db_high_availability = false
db_disk_size_gb      = 20
db_max_connections   = 200
redis_tier           = "BASIC"
redis_memory_gb      = 1

# CI syncs the demo corpus (data/rag_docs) into the staging RAG bucket.
ci_manages_rag_docs = true
# The production deployer promotes staging-verified image digests by pulling them from here.
artifact_readers = ["serviceAccount:gh-deployer@my-ordering-prod.iam.gserviceaccount.com"]

deletion_protection = false
