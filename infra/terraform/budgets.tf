data "google_project" "current" {
  count      = var.billing_account_id != "" ? 1 : 0
  project_id = var.project_id
}

# Monthly GCP Billing Budget alert (provisioned when billing_account_id is supplied).
# Fires notifications at 20%, 50%, 80%, and 100% of actual spend plus 80% forecasted spend.
resource "google_billing_budget" "monthly_guardrail" {
  count           = var.billing_account_id != "" ? 1 : 0
  billing_account = trimprefix(trimspace(var.billing_account_id), "billingAccounts/")
  display_name = (
    var.budget_currency_code != ""
    ? format("Agentic Emailer Monthly Budget (%s %s)", var.monthly_budget_usd, var.budget_currency_code)
    : format("Agentic Emailer Monthly Budget (%s)", var.monthly_budget_usd)
  )

  budget_filter {
    projects = ["projects/${data.google_project.current[0].number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency_code != "" ? var.budget_currency_code : null
      units         = tostring(var.monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.2
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.5
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.8
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "CURRENT_SPEND"
  }

  threshold_rules {
    threshold_percent = 0.8
    spend_basis       = "FORECASTED_SPEND"
  }

  depends_on = [google_project_service.services["billingbudgets.googleapis.com"]]
}
