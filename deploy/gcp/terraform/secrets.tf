# Secret Manager is the single source of truth for secrets. Pods mount them as files through the
# GKE Secret Manager CSI add-on (deploy/k8s/base/secretproviderclasses.yaml).
#
# Generated secrets get a version here. The three LLM provider keys are created EMPTY: add them
# out of band so they never enter Terraform state:
#   deploy/gcp/scripts/set-llm-keys.sh <project>
locals {
  redis_ca_path = "/run/secrets/ordering/redis_ca.pem"

  generated_secrets = {
    "ordering-database-url" = format(
      "postgresql+psycopg://app_user:%s@%s:5432/%s?sslmode=require",
      random_password.db_app.result,
      google_sql_database_instance.this.private_ip_address,
      google_sql_database.ordering.name,
    )
    "ordering-database-admin-url" = format(
      "postgresql+psycopg://%s:%s@%s:5432/%s?sslmode=require",
      google_sql_user.owner.name,
      random_password.db_owner.result,
      google_sql_database_instance.this.private_ip_address,
      google_sql_database.ordering.name,
    )
    "ordering-app-db-password" = random_password.db_app.result
    # The CA is verified; the Memorystore certificate is issued for the instance, not the IP, so
    # hostname checking is off.
    "ordering-redis-url" = format(
      "rediss://:%s@%s:%d/0?ssl_cert_reqs=required&ssl_ca_certs=%s&ssl_check_hostname=false",
      google_redis_instance.this.auth_string,
      google_redis_instance.this.host,
      google_redis_instance.this.port,
      local.redis_ca_path,
    )
    "ordering-redis-ca"           = join("\n", [for c in google_redis_instance.this.server_ca_certs : c.cert])
    "ordering-jwt-secret"         = random_password.jwt.result
    "ordering-service-jwt-secret" = random_password.service_jwt.result
    "ordering-litellm-api-key"    = "sk-${random_password.litellm.result}"
  }

  external_secrets = ["ordering-anthropic-api-key", "ordering-openai-api-key", "ordering-google-api-key"]
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "service_jwt" {
  length  = 64
  special = false
}

resource "random_password" "litellm" {
  length  = 40
  special = false
}

resource "google_secret_manager_secret" "generated" {
  for_each  = local.generated_secrets
  secret_id = each.key
  replication {
    user_managed {
      replicas {
        location = var.region # data residency: secrets stay in the deployment region
      }
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "generated" {
  for_each    = local.generated_secrets
  secret      = google_secret_manager_secret.generated[each.key].id
  secret_data = each.value
}

resource "google_secret_manager_secret" "external" {
  for_each  = toset(local.external_secrets)
  secret_id = each.key
  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }
  depends_on = [google_project_service.apis]
}
