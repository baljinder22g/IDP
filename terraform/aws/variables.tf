variable "project" {
  description = "Name prefix for all resources"
  type        = string
  default     = "idp-studio"
}

variable "region" {
  description = "AWS region. Bedrock + Textract must both be available here (e.g. us-east-1, us-west-2, eu-west-1)."
  type        = string
  default     = "us-east-1"
}

variable "bedrock_model" {
  description = "Default Bedrock model id (inference profile) used by the Lambda. Use a cross-region profile id like 'us.anthropic.claude-...' or 'global.anthropic.claude-...'; bare on-demand ids are not supported."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_api_key" {
  description = <<-EOT
    OPTIONAL Bedrock API key (bearer token). If set, the Lambda authenticates to
    Bedrock with this token (AWS_BEARER_TOKEN_BEDROCK) instead of its IAM role —
    handy when the account hasn't completed the AWS Marketplace model
    subscription (e.g. no payment method yet). Prefer a LONG-TERM key here;
    short-term keys expire (~12h). Leave empty to use the IAM role, or to supply
    a key per-request from the app's Settings instead.
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "allowed_origin" {
  description = "CORS origin allowed to call the API. Set to your GitHub Pages URL, e.g. https://USER.github.io"
  type        = string
  default     = "*"
}

variable "api_key" {
  description = "Optional shared secret. If non-empty, callers must send it as x-api-key. Leave empty to disable."
  type        = string
  default     = ""
  sensitive   = true
}

variable "log_retention_days" {
  description = "Days to keep CloudWatch logs"
  type        = number
  default     = 14
}

# ── Cost guardrails ──────────────────────────────────────────────────────────
variable "monthly_budget_usd" {
  description = "Monthly AWS cost budget (USD) for this account. An alert email is sent at 50%, 80% and 100% of this amount. Set to 0 to skip creating the budget."
  type        = number
  default     = 10
}

variable "budget_alert_email" {
  description = "Email address to receive budget alerts. Required if monthly_budget_usd > 0."
  type        = string
  default     = ""
}

variable "bedrock_max_tokens" {
  description = "Max output tokens per Bedrock call (caps per-request cost)."
  type        = number
  default     = 4096
}
