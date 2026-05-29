###############################################################################
# IDP Studio — AWS backend
# Creates: S3 (input + logs), IAM role, 4 Lambdas, HTTP API Gateway, CORS.
# Implements the contract in api/openapi.yaml for: bedrock, textract, agents, logs.
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

data "aws_caller_identity" "current" {}

resource "random_id" "suffix" {
  byte_length = 3
}

locals {
  name        = "${var.project}-${random_id.suffix.hex}"
  input_bucket = "${local.name}-input"
  log_bucket   = "${local.name}-logs"
  lambda_env = {
    LOG_BUCKET     = aws_s3_bucket.logs.bucket
    INPUT_BUCKET   = aws_s3_bucket.input.bucket
    BEDROCK_MODEL  = var.bedrock_model
    ALLOWED_ORIGIN = var.allowed_origin
    API_KEY        = var.api_key
  }
}

###############################################################################
# S3 buckets
###############################################################################
resource "aws_s3_bucket" "input" {
  bucket        = local.input_bucket
  force_destroy = true
}

resource "aws_s3_bucket" "logs" {
  bucket        = local.log_bucket
  force_destroy = true
}

# Block all public access on both buckets
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

# Auto-expire uploaded documents (keep storage free-tier friendly)
resource "aws_s3_bucket_lifecycle_configuration" "input" {
  bucket = aws_s3_bucket.input.id
  rule {
    id     = "expire-inputs"
    status = "Enabled"
    expiration { days = 1 }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    id     = "expire-logs"
    status = "Enabled"
    expiration { days = 30 }
  }
}

###############################################################################
# IAM role for Lambdas
###############################################################################
resource "aws_iam_role" "lambda" {
  name = "${local.name}-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
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
        Sid    = "Logs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid    = "S3"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.input.arn, "${aws_s3_bucket.input.arn}/*",
          aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*",
        ]
      },
      {
        Sid    = "Bedrock"
        Effect = "Allow"
        Action = ["bedrock:InvokeModel", "bedrock:Converse"]
        Resource = "*"
      },
      {
        Sid    = "Textract"
        Effect = "Allow"
        Action = ["textract:StartDocumentAnalysis", "textract:GetDocumentAnalysis", "textract:AnalyzeDocument"]
        Resource = "*"
      }
    ]
  })
}

###############################################################################
# Lambda packaging — zip the whole lambda/ folder once, reuse for all functions
###############################################################################
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/build/lambda.zip"
}

locals {
  functions = {
    bedrock  = { handler = "extract_bedrock.handler",      timeout = 60 }
    textract = { handler = "extract_textract.handler",     timeout = 120 }
    agents   = { handler = "agents_orchestrator.handler",  timeout = 180 }
    logs     = { handler = "logs.handler",                 timeout = 30 }
  }
}

resource "aws_lambda_function" "fn" {
  for_each         = local.functions
  function_name    = "${local.name}-${each.key}"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = each.value.handler
  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256
  timeout          = each.value.timeout
  memory_size      = 512
  environment { variables = local.lambda_env }
}

resource "aws_cloudwatch_log_group" "fn" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = var.log_retention_days
}

###############################################################################
# HTTP API Gateway (v2) with built-in CORS
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

resource "aws_apigatewayv2_integration" "fn" {
  for_each               = local.functions
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.fn[each.key].invoke_arn
  payload_format_version = "2.0"
}

locals {
  routes = {
    bedrock  = "POST /v1/extract/bedrock"
    textract = "POST /v1/extract/textract"
    agents   = "POST /v1/agents/run"
    logs     = "GET /v1/logs"
  }
}

resource "aws_apigatewayv2_route" "r" {
  for_each  = local.routes
  api_id    = aws_apigatewayv2_api.api.id
  route_key = each.value
  target    = "integrations/${aws_apigatewayv2_integration.fn[each.key].id}"
}

resource "aws_apigatewayv2_stage" "prod" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "prod"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 20
  }
}

resource "aws_lambda_permission" "apigw" {
  for_each      = local.functions
  statement_id  = "AllowAPIGW-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fn[each.key].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
