variable "project_id" {
  description = "GCP project ID where Agentic Emailer resources will be provisioned."
  type        = string

  validation {
    condition     = length(trimspace(var.project_id)) > 0
    error_message = "project_id must be a non-empty GCP project ID."
  }
}

variable "billing_account_id" {
  description = "Optional GCP Billing Account ID (format: XXXXXX-XXXXXX-XXXXXX). When set, provisions monthly budget alerts."
  type        = string
  default     = ""
}

variable "region" {
  description = "GCP region for Cloud Run Job, Firestore, Artifact Registry, and Cloud Scheduler. Defaults to us-central1 (Tier-1 Always Free)."
  type        = string
  default     = "us-central1"

  validation {
    condition     = contains(["us-central1", "us-east1", "us-west1"], var.region)
    error_message = "region must be a Tier-1 Always Free region (us-central1, us-east1, or us-west1) to preserve GCP Always Free eligibility."
  }
}

variable "github_repo" {
  description = "GitHub repository in 'owner/repo' format allowed to deploy via keyless OIDC Workload Identity Federation."
  type        = string
  default     = "siddhant-ban/agentic-ai-digest"

  validation {
    condition     = can(regex("^[^/\\s]+/[^/\\s]+$", var.github_repo))
    error_message = "github_repo must be in 'owner/repo' format."
  }
}

variable "smtp_sender" {
  description = "Sender email address for SMTP delivery (e.g., your Gmail address)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.smtp_sender))
    error_message = "smtp_sender must be a valid email address."
  }
}

variable "schedule_cron" {
  description = "Cron schedule for the daily Cloud Scheduler trigger (default: 12:00 UTC daily)."
  type        = string
  default     = "0 12 * * *"
}

variable "schedule_timezone" {
  description = "IANA timezone for Cloud Scheduler cron evaluation."
  type        = string
  default     = "Etc/UTC"
}

variable "research_model" {
  description = "Vertex AI Gemini model used by the ADK ResearchAgent."
  type        = string
  default     = "gemini-3.1-flash-lite"
}

variable "synthesis_model" {
  description = "Vertex AI Gemini model used by the ADK SynthesisAgent."
  type        = string
  default     = "gemini-3.1-flash-lite"
}

variable "critic_model" {
  description = "Vertex AI Gemini model used by the ADK CriticAgent."
  type        = string
  default     = "gemini-3.1-flash-lite"
}

variable "monthly_budget_usd" {
  description = "Monthly GCP billing budget cap in USD for alert notifications (matches $10/mo Google AI Pro credits)."
  type        = number
  default     = 10

  validation {
    condition     = var.monthly_budget_usd >= 1
    error_message = "monthly_budget_usd must be at least 1 USD."
  }
}
