variable "project" {
  description = "Name prefix (lowercase letters/numbers; storage account names are derived from this)"
  type        = string
  default     = "idpstudio"
}

variable "location" {
  description = "Azure region for storage + Cognitive Services (Document Intelligence + Azure OpenAI). gpt-4o must be available here (eastus, westeurope, etc.)."
  type        = string
  default     = "eastus"
}

variable "functions_location" {
  description = <<-EOT
    Region for the App Service plan + Function App (compute). New subscriptions
    often have 0 App Service quota in popular regions (eastus/eastus2/westus2).
    Verified-working regions on this subscription: westus3, centralus, westeurope.
    Can differ from var.location — the Function App reaches Cognitive Services
    over HTTPS regardless of region.
  EOT
  type    = string
  default = "westus3"
}

variable "allowed_origin" {
  description = "CORS origin allowed to call the Function App, e.g. https://USER.github.io"
  type        = string
  default     = "*"
}

variable "api_key" {
  description = "Optional shared secret required as x-api-key. Leave empty to disable."
  type        = string
  default     = ""
  sensitive   = true
}

variable "docint_sku" {
  description = "Document Intelligence SKU. F0 = free tier (limited), S0 = standard."
  type        = string
  default     = "F0"
}

variable "openai_model" {
  description = "Azure OpenAI model to deploy for schema mapping"
  type        = string
  default     = "gpt-4o"
}

variable "openai_model_version" {
  description = "Must be a non-deprecated version. Check with: az cognitiveservices account list-models ..."
  type        = string
  default     = "2024-11-20"
}

variable "deploy_openai" {
  description = "Set false if your subscription does not yet have Azure OpenAI access (request it first)."
  type        = bool
  default     = true
}

variable "functions_plan_sku" {
  description = <<-EOT
    App Service plan SKU for the Function App.
      Y1 = Linux Consumption (cheapest, pay-per-execution) BUT new subscriptions
           often have 0 quota for it ("Total VMs: 0" error).
      B1 = Basic dedicated (~$13/mo) — uses regular regional vCPU quota that new
           subscriptions already have, so it deploys immediately.
    Python Functions require a Linux plan in both cases.
  EOT
  type    = string
  default = "B1"
}
