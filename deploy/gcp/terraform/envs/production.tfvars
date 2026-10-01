# terraform -chdir=deploy/gcp/terraform init -backend-config=envs/production.gcs.tfbackend
# terraform -chdir=deploy/gcp/terraform apply -var-file=envs/production.tfvars
project_id        = "my-ordering-prod"
environment       = "production"
region            = "europe-west2"
app_host          = "ordering.example.com"
github_repository = "my-org/ordering-agentic-system"

db_tier                  = "db-custom-4-15360"
db_high_availability     = true
db_disk_size_gb          = 100
db_max_connections       = 500
db_backup_retention_days = 30
redis_tier               = "STANDARD_HA"
redis_memory_gb          = 4

ci_manages_rag_docs = false
deletion_protection = true
