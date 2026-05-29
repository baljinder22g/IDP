output "function_app_name" {
  description = "Use this with: func azure functionapp publish <name>"
  value       = azurerm_linux_function_app.fn.name
}

output "api_base_url" {
  description = "Paste into IDP Studio → Settings → API base URL (append nothing; routes are /v1/...)"
  value       = "https://${azurerm_linux_function_app.fn.default_hostname}/api"
}

output "docint_endpoint" {
  value = azurerm_cognitive_account.docint.endpoint
}

output "resource_group" {
  value = azurerm_resource_group.rg.name
}

output "next_steps" {
  value = <<-EOT
    1. Publish the function code:
         cd terraform/azure/function
         func azure functionapp publish ${azurerm_linux_function_app.fn.name}
    2. Get the function key (Portal > Function App > App keys) and append to
       the API base URL as ?code=<key>, OR call with the x-api-key you set.
    3. In IDP Studio > Settings set API base URL to:
         https://${azurerm_linux_function_app.fn.default_hostname}/api
  EOT
}
