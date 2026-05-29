variable "project" {
  description = "Name prefix (lowercase letters/numbers; storage account names are derived from this)"
  type        = string
  default     = "idpstudio"
}

variable "location" {
  description = "Azure region. Document Intelligence + Azure OpenAI must be available here (e.g. eastus, westeurope)."
  type        = string
  default     = "eastus"
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
  type    = string
  default = "2024-08-06"
}

variable "deploy_openai" {
  description = "Set false if your subscription does not yet have Azure OpenAI access (request it first)."
  type        = bool
  default     = true
}
