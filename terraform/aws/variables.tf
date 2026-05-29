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
  description = "Default Bedrock model id used by the Lambdas"
  type        = string
  default     = "anthropic.claude-sonnet-4-6-v1:0"
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
