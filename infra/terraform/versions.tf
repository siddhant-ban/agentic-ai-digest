terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Optional GCS remote state backend for team or multi-machine workflows.
  # For single-developer bootstrap, local state works out of the box.
  # To migrate to GCS later, create a versioned bucket and uncomment:
  #
  # backend "gcs" {
  #   bucket = "YOUR_PROJECT_ID-terraform-state"
  #   prefix = "agentic-emailer/prod"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
