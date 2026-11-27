locals {
  enabled_services = toset([
    "aiplatform.googleapis.com",
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudscheduler.googleapis.com",
    "cloudtrace.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "sts.googleapis.com",
  ])
}

resource "google_project_service" "services" {
  for_each = local.enabled_services

  project                    = var.project_id
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

# Artifact Registry Docker repository with strict cleanup policies to stay
# well below the 0.5 GiB GCP Always Free storage limit.
resource "google_artifact_registry_repository" "docker" {
  project                = var.project_id
  location               = var.region
  repository_id          = "agentic-emailer"
  description            = "Container images for Agentic Emailer Cloud Run Job"
  format                 = "DOCKER"
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent-3"
    action = "KEEP"

    most_recent_versions {
      keep_count = 3
    }
  }

  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"

    condition {
      tag_state  = "UNTAGGED"
      older_than = "86400s"
    }
  }

  cleanup_policies {
    id     = "delete-old-tagged"
    action = "DELETE"

    condition {
      tag_state  = "TAGGED"
      older_than = "86400s"
    }
  }

  depends_on = [google_project_service.services["artifactregistry.googleapis.com"]]
}

# Firestore Native database in Tier-1 Always Free region (us-central1).
# Retention cleanup (>30 days) is performed programmatically to avoid managed TTL billing nuances.
resource "google_firestore_database" "default" {
  project                 = var.project_id
  name                    = "(default)"
  location_id             = var.region
  type                    = "FIRESTORE_NATIVE"
  delete_protection_state = "DELETE_PROTECTION_DISABLED"

  depends_on = [google_project_service.services["firestore.googleapis.com"]]
}

# Secret Manager shell for SMTP password ONLY (Vertex AI uses IAM, not API keys).
# Add the secret version out-of-band via `gcloud secrets versions add` so plaintext
# credentials never touch Terraform state. Keep <= 6 active versions for Always Free.
resource "google_secret_manager_secret" "smtp_password" {
  project   = var.project_id
  secret_id = "agentic-emailer-smtp-password"

  replication {
    auto {}
  }

  depends_on = [google_project_service.services["secretmanager.googleapis.com"]]
}
