output "api_base_url" {
  description = "Paste this into the IDP Studio app → Settings → API base URL"
  value       = aws_apigatewayv2_stage.prod.invoke_url
}

output "input_bucket" {
  description = "S3 bucket for uploaded documents (auto-expires in 1 day)"
  value       = aws_s3_bucket.input.bucket
}

output "log_bucket" {
  description = "S3 bucket for processing logs (shown in the Logs tab)"
  value       = aws_s3_bucket.logs.bucket
}

output "region" {
  value = var.region
}

output "next_steps" {
  value = <<-EOT
    1. Open the IDP Studio app, click the gear (Settings).
    2. Set 'API base URL' to:  ${aws_apigatewayv2_stage.prod.invoke_url}
    3. If you set an api_key, paste the same value into the API key field.
    4. Make sure Bedrock model access is enabled in region '${var.region}'
       (AWS Console > Bedrock > Model access > enable the Claude models).
  EOT
}
