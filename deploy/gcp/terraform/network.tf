resource "google_compute_network" "vpc" {
  name                    = local.name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "gke" {
  name                     = "${local.name}-gke"
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = var.pods_cidr
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = var.services_cidr
  }

  log_config {
    aggregation_interval = "INTERVAL_5_SEC"
    flow_sampling        = 0.5
    metadata             = "INCLUDE_ALL_METADATA"
  }
}

# Private nodes reach the internet only via Cloud NAT (LLM provider APIs from llm-gateway).
resource "google_compute_router" "nat" {
  name    = "${local.name}-router"
  network = google_compute_network.vpc.id
  region  = var.region
}

resource "google_compute_router_nat" "nat" {
  name                               = "${local.name}-nat"
  router                             = google_compute_router.nat.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# Private Service Access: Cloud SQL and Memorystore get private IPs from this range.
resource "google_compute_global_address" "private_services" {
  name          = "${local.name}-private-services"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  address       = split("/", var.private_services_cidr)[0]
  prefix_length = tonumber(split("/", var.private_services_cidr)[1])
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "private_services" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services.name]
}

# *.googleapis.com -> private.googleapis.com (199.36.153.8/30), so pods reach Google APIs on a
# fixed range that the NetworkPolicies can allow without opening the internet.
resource "google_dns_managed_zone" "googleapis" {
  name       = "${local.name}-googleapis"
  dns_name   = "googleapis.com."
  visibility = "private"
  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc.id
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_dns_record_set" "googleapis_a" {
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "private.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"]
}

resource "google_dns_record_set" "googleapis_cname" {
  managed_zone = google_dns_managed_zone.googleapis.name
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["private.googleapis.com."]
}
