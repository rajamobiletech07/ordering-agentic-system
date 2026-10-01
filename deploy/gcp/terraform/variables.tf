variable "project_id" {
  description = "GCP project for this environment (one project per environment is recommended)."
  type        = string
}

variable "environment" {
  description = "staging | production"
  type        = string
  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "region" {
  description = "Region for GKE, Cloud SQL, Memorystore, Artifact Registry and buckets."
  type        = string
  default     = "europe-west2"
}

variable "app_host" {
  description = "Public hostname for the API (DNS A record -> static IP output)."
  type        = string
}

variable "github_repository" {
  description = "owner/repo allowed to deploy via Workload Identity Federation."
  type        = string
}

# --- network ------------------------------------------------------------------------------
variable "subnet_cidr" {
  type    = string
  default = "10.10.0.0/20"
}

variable "pods_cidr" {
  type    = string
  default = "10.20.0.0/14"
}

variable "services_cidr" {
  type    = string
  default = "10.24.0.0/20"
}

variable "private_services_cidr" {
  description = "Private Service Access range for Cloud SQL + Memorystore (also used by NetworkPolicies)."
  type        = string
  default     = "10.100.0.0/16"
}

variable "control_plane_authorized_cidrs" {
  description = "Optional CIDRs allowed to reach the IP-based control-plane endpoint. CI uses the DNS endpoint (IAM-authorized), so this can stay empty."
  type        = list(object({ cidr = string, name = string }))
  default     = []
}

# --- Cloud SQL ------------------------------------------------------------------------------
variable "db_tier" {
  type    = string
  default = "db-custom-2-7680"
}

variable "db_high_availability" {
  description = "Regional (multi-zone) Cloud SQL instance with automatic failover."
  type        = bool
  default     = true
}

variable "db_disk_size_gb" {
  type    = number
  default = 50
}

variable "db_max_connections" {
  description = "Sum of (pods x DB_POOL_SIZE) across services must stay below this."
  type        = number
  default     = 400
}

variable "db_backup_retention_days" {
  type    = number
  default = 14
}

# --- Memorystore ----------------------------------------------------------------------------
variable "redis_tier" {
  description = "BASIC (staging) or STANDARD_HA (production, replica + automatic failover)."
  type        = string
  default     = "STANDARD_HA"
}

variable "redis_memory_gb" {
  type    = number
  default = 2
}

# --- delivery -------------------------------------------------------------------------------
variable "artifact_readers" {
  description = "Extra principals allowed to pull images from this environment's registry (e.g. the production deployer, which promotes staging-verified digests)."
  type        = list(string)
  default     = []
}

variable "ci_manages_rag_docs" {
  description = "Let the deployer sync data/rag_docs into the RAG bucket (staging demo corpus). Keep false in production, where content owners manage documents."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "armor_rate_limit_per_minute" {
  description = "Cloud Armor per-client-IP request budget before throttling (edge backstop; the API gateway enforces per-user limits)."
  type        = number
  default     = 300
}
