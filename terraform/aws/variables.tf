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
  type      = string
  default   = ""
  sensitive = true
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
