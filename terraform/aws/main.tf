###############################################################################
# IDP Studio — AWS backend
#
# What this creates (one of each):
#   2 × S3 bucket      — document input (1-day expiry) + processing logs (30-day)
#   1 × IAM role       — least-privilege role for the Lambda
#   1 × Lambda         — handler.py routes all 4 API paths in one function
#   1 × API Gateway    — HTTP API with 4 routes, CORS, throttling
#   1 × CloudWatch log group
#
# Usage:
#   terraform init
#   terraform apply
#   → copy api_base_url output → paste into IDP Studio → Settings
###############################################################################

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.40" }
    archive = { source = "hashicorp/archive", version = "~> 2.4" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.region
}

resource "random_id" "suffix" {
  byte_length = 3 # adds a 6-char hex suffix so bucket names are globally unique
}

locals {
  name = "${var.project}-${random_id.suffix.hex}"
}

###############################################################################
# S3 — two private buckets
###############################################################################

resource "aws_s3_bucket" "input" {
  bucket        = "${local.name}-input"
  force_destroy = true
}

resource "aws_s3_bucket" "logs" {
  bucket        = "${local.name}-logs"
  force_destroy = true
}

# Block all public access (these buckets are only touched by Lambda)
resource "aws_s3_bucket_public_access_block" "input" {
  bucket                  = aws_s3_bucket.input.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Auto-delete uploaded PDFs after 1 day; logs after 30 days
resource "aws_s3_bucket_lifecycle_configuration" "input" {
  bucket = aws_s3_bucket.input.id
  rule {
    id     = "expire-inputs"
    status = "Enabled"
    filter {}
    expiration { days = 1 }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire-logs"
    status = "Enabled"
    filter {}
    expiration { days = 30 }
  }
}

###############################################################################
# IAM — Lambda execution role
###############################################################################

resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.name}-lambda-policy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CloudWatchLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid    = "S3Access"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.input.arn, "${aws_s3_bucket.input.arn}/*",
          aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*",
        ]
      },
      {
        Sid    = "BedrockAccess"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream",
          "bedrock:GetFoundationModel",
          "bedrock:GetInferenceProfile",
          "bedrock:ListInferenceProfiles",
        ]
        Resource = "*"
      },
      {
        # New Bedrock access flow: foundation models are served via AWS
        # Marketplace and auto-enabled on first invoke. The calling principal
        # needs these marketplace actions to complete that one-time subscription.
        Sid      = "BedrockMarketplace"
        Effect   = "Allow"
        Action   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
        Resource = "*"
      },
      {
        Sid      = "TextractAccess"
        Effect   = "Allow"
        Action   = ["textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis", "textract:AnalyzeDocument"]
        Resource = "*"
      },
      {
        # PII/PHI masking in the agentic pipeline (existing managed models, no training)
        Sid      = "ComprehendAccess"
        Effect   = "Allow"
        Action   = ["comprehend:DetectPiiEntities", "comprehendmedical:DetectPHI"]
        Resource = "*"
      }
    ]
  })
}

###############################################################################
# Lambda — single function that handles all routes
###############################################################################

# Zip the entire lambda/ directory (handler.py + common.py)
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/build/lambda.zip"
  excludes    = ["__pycache__", ".gitignore"]
}

resource "aws_lambda_function" "idp" {
  function_name    = "${local.name}-handler"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "handler.handler" # file: handler.py, function: handler()
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = 180 # 3 min — covers Textract async polling
  memory_size      = 512

  environment {
    variables = {
      LOG_BUCKET         = aws_s3_bucket.logs.bucket
      INPUT_BUCKET       = aws_s3_bucket.input.bucket
      BEDROCK_MODEL      = var.bedrock_model
      ALLOWED_ORIGIN     = var.allowed_origin
      API_KEY            = var.api_key
      BEDROCK_API_KEY    = var.bedrock_api_key # optional baked-in bearer token (see variables.tf)
      BEDROCK_MAX_TOKENS = tostring(var.bedrock_max_tokens)
    }
  }
}

resource "aws_cloudwatch_log_group" "idp" {
  name              = "/aws/lambda/${aws_lambda_function.idp.function_name}"
  retention_in_days = var.log_retention_days
}

###############################################################################
# API Gateway (HTTP API v2) — one gateway, four routes, all → one Lambda
###############################################################################

resource "aws_apigatewayv2_api" "api" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"
  cors_configuration {
    allow_origins = [var.allowed_origin]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type", "x-api-key"]
    max_age       = 3600
  }
}

# One integration → the single Lambda
resource "aws_apigatewayv2_integration" "idp" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.idp.invoke_arn
  payload_format_version = "2.0"
}

# Four routes — all pointing to the same integration
resource "aws_apigatewayv2_route" "bedrock" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /v1/extract/bedrock"
  target    = "integrations/${aws_apigatewayv2_integration.idp.id}"
}

resource "aws_apigatewayv2_route" "textract" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /v1/extract/textract"
  target    = "integrations/${aws_apigatewayv2_integration.idp.id}"
}

resource "aws_apigatewayv2_route" "agents" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /v1/agents/run"
  target    = "integrations/${aws_apigatewayv2_integration.idp.id}"
}

resource "aws_apigatewayv2_route" "agents_external" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /v1/agents/run-external"
  target    = "integrations/${aws_apigatewayv2_integration.idp.id}"
}

resource "aws_apigatewayv2_route" "agents_prepare" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /v1/agents/prepare"
  target    = "integrations/${aws_apigatewayv2_integration.idp.id}"
}

resource "aws_apigatewayv2_route" "agents_extract" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /v1/agents/extract"
  target    = "integrations/${aws_apigatewayv2_integration.idp.id}"
}

resource "aws_apigatewayv2_route" "logs" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "GET /v1/logs"
  target    = "integrations/${aws_apigatewayv2_integration.idp.id}"
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "prod"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10 # max concurrent requests
    throttling_rate_limit  = 20 # requests per second
  }
}

# Allow API Gateway to invoke the Lambda
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.idp.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

###############################################################################
# Cost guardrail — monthly AWS Budget with email alerts (Bedrock + Textract are
# the only metered services here). Created only when a budget + email are set.
###############################################################################
resource "aws_budgets_budget" "monthly" {
  count        = var.monthly_budget_usd > 0 && var.budget_alert_email != "" ? 1 : 0
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100] # alert at 50%, 80%, 100% of the budget
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
