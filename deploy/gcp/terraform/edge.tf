# Edge resources that the GKE Ingress (deploy/k8s/base/ingress.yaml) attaches to by name.
resource "google_compute_global_address" "api" {
  name       = "${local.name}-ip"
  depends_on = [google_project_service.apis]
}

resource "google_compute_ssl_policy" "tls" {
  name            = "${local.name}-tls"
  profile         = "MODERN"
  min_tls_version = "TLS_1_2"
}

# Cloud Armor in front of the API: OWASP CRS rules, a per-IP rate backstop and Adaptive Protection
# (L7 DDoS). The API gateway still enforces per-user limits and all authorization.
resource "google_compute_security_policy" "edge" {
  name        = "${local.name}-edge"
  description = "Ordering API edge policy"

  adaptive_protection_config {
    layer_7_ddos_defense_config {
      enable = true
    }
  }

  advanced_options_config {
    json_parsing = "STANDARD" # inspect JSON bodies field-by-field (fewer false positives on chat text)
    log_level    = "VERBOSE"
  }

  dynamic "rule" {
    for_each = {
      1000 = "sqli-v33-stable"
      1001 = "xss-v33-stable"
      1002 = "lfi-v33-stable"
      1003 = "rce-v33-stable"
      1004 = "protocolattack-v33-stable"
      1005 = "scannerdetection-v33-stable"
    }
    content {
      priority    = rule.key
      action      = "deny(403)"
      description = "OWASP CRS ${rule.value}"
      match {
        expr {
          expression = "evaluatePreconfiguredWaf('${rule.value}', {'sensitivity': 1})"
        }
      }
    }
  }

  rule {
    priority    = 2000
    action      = "throttle"
    description = "Per-client-IP rate backstop"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
    rate_limit_options {
      conform_action = "allow"
      exceed_action  = "deny(429)"
      enforce_on_key = "IP"
      rate_limit_threshold {
        count        = var.armor_rate_limit_per_minute
        interval_sec = 60
      }
    }
  }

  rule {
    priority    = 2147483647
    action      = "allow"
    description = "default allow"
    match {
      versioned_expr = "SRC_IPS_V1"
      config {
        src_ip_ranges = ["*"]
      }
    }
  }
}
