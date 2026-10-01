#!/usr/bin/env bash
# One-time, per environment: enable the minimum APIs and create the Terraform state bucket.
# Everything else (network, GKE, Cloud SQL, WIF for GitHub, ...) is created by Terraform.
#
#   deploy/gcp/scripts/bootstrap.sh <project-id> [region]
#
# Run it as a human with Owner (or equivalent) on the project. The first `terraform apply` is also
# run locally; later applies go through .github/workflows/terraform.yml.
set -euo pipefail

PROJECT="${1:?usage: bootstrap.sh <project-id> [region]}"
REGION="${2:-europe-west2}"
BUCKET="${PROJECT}-tfstate"

gcloud services enable --project "$PROJECT" \
  cloudresourcemanager.googleapis.com serviceusage.googleapis.com iam.googleapis.com storage.googleapis.com

if ! gcloud storage buckets describe "gs://$BUCKET" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --project "$PROJECT" --location "$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets update "gs://$BUCKET" --versioning

cat <<EOF
State bucket ready: gs://$BUCKET
Next:
  1. Set project_id / app_host / github_repository in deploy/gcp/terraform/envs/<env>.tfvars
     and bucket = "$BUCKET" in envs/<env>.gcs.tfbackend
  2. terraform -chdir=deploy/gcp/terraform init -backend-config=envs/<env>.gcs.tfbackend
  3. terraform -chdir=deploy/gcp/terraform apply -var-file=envs/<env>.tfvars
  4. deploy/gcp/scripts/set-llm-keys.sh $PROJECT
  5. deploy/gcp/scripts/write-values.sh <env>   (then commit deploy/k8s/overlays/<env>/values.env)
EOF
