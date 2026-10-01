# --- Cloud SQL for PostgreSQL 16 (+ pgvector) -------------------------------------------------
resource "google_sql_database_instance" "this" {
  name                = local.name
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.deletion_protection
  depends_on          = [google_service_networking_connection.private_services]

  settings {
    edition           = "ENTERPRISE"
    tier              = var.db_tier
    availability_type = var.db_high_availability ? "REGIONAL" : "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.db_disk_size_gb
    disk_autoresize   = true

    ip_configuration {
      ipv4_enabled                                  = false # private IP only
      private_network                               = google_compute_network.vpc.id
      ssl_mode                                      = "ENCRYPTED_ONLY"
      enable_private_path_for_google_cloud_services = true
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
      start_time                     = "01:00"
      transaction_log_retention_days = 7
      backup_retention_settings {
        retained_backups = var.db_backup_retention_days
      }
    }

    maintenance_window {
      day          = 7
      hour         = 3
      update_track = "stable"
    }

    insights_config {
      query_insights_enabled  = true
      record_application_tags = true
      record_client_address   = false
    }

    database_flags {
      name  = "max_connections"
      value = tostring(var.db_max_connections)
    }
    database_flags {
      name  = "log_min_duration_statement"
      value = "1000"
    }
    database_flags {
      name  = "cloudsql.enable_pgaudit"
      value = "on"
    }
    # CIS PostgreSQL logging baseline (to Cloud Logging, for audit and forensics).
    database_flags {
      name  = "log_connections"
      value = "on"
    }
    database_flags {
      name  = "log_disconnections"
      value = "on"
    }
    database_flags {
      name  = "log_lock_waits"
      value = "on"
    }
    database_flags {
      name  = "log_checkpoints"
      value = "on"
    }
    database_flags {
      name  = "log_temp_files"
      value = "0"
    }
  }
}

resource "google_sql_database" "ordering" {
  name     = "ordering"
  instance = google_sql_database_instance.this.name
}

# Owner role: migrations, seeding and ingestion (db-migrate / rag-ingest jobs only). The runtime
# role `app_user` is created in SQL by scripts/db_bootstrap.py rather than through the Cloud SQL
# API, so it doesn't inherit cloudsqlsuperuser and RLS applies to it.
resource "random_password" "db_owner" {
  length  = 32
  special = false
}

resource "google_sql_user" "owner" {
  name     = "ordering"
  instance = google_sql_database_instance.this.name
  password = random_password.db_owner.result
}

resource "random_password" "db_app" {
  length  = 32
  special = false
}

# --- Memorystore for Redis 7 ------------------------------------------------------------------
resource "google_redis_instance" "this" {
  name                    = local.name
  region                  = var.region
  tier                    = var.redis_tier
  memory_size_gb          = var.redis_memory_gb
  redis_version           = "REDIS_7_2"
  authorized_network      = google_compute_network.vpc.id
  connect_mode            = "PRIVATE_SERVICE_ACCESS"
  auth_enabled            = true
  transit_encryption_mode = "SERVER_AUTHENTICATION" # TLS on :6378, CA published below
  redis_configs = {
    "maxmemory-policy" = "volatile-lru" # thread caches/locks all carry TTLs (same as compose)
  }

  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 3
        minutes = 0
      }
    }
  }

  depends_on = [google_service_networking_connection.private_services]
}

# --- Cloud Storage ------------------------------------------------------------------------------
resource "google_storage_bucket" "rag_docs" {
  name                        = "${var.project_id}-ordering-rag-docs"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  versioning {
    enabled = true # document history, alongside the RAG pipeline's own version table
  }
}

resource "google_storage_bucket" "agentops" {
  name                        = "${var.project_id}-ordering-agentops"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle_rule {
    condition {
      age = 180 # matches MEMORY_RETENTION_DAYS; exported candidates are already redacted
    }
    action {
      type = "Delete"
    }
  }
}
