# Active settings for THIS subscription (discovered during deployment).
# See terraform.tfvars.example for the full list of options.

location           = "eastus"     # storage + Cognitive Services (gpt-4o region)
functions_location = "westus3"    # App Service quota only exists here on this sub
functions_plan_sku = "B1"         # new sub has 0 Consumption (Y1) quota
allowed_origin     = "*"

# Document Intelligence: F0 (free, limited) or S0 (standard / pay-as-you-go).
docint_sku = "F0"

# Azure OpenAI: this new subscription has 0 TPM quota for ALL chat models.
# Request a quota increase at https://aka.ms/oai/quotaincrease, then set this
# to true and re-run `terraform apply`. Until then the Azure tab returns raw
# Document Intelligence content instead of schema-mapped JSON.
deploy_openai = false

location       = "eastus"
# allowed_origin = "https://YOUR_GITHUB_USERNAME.github.io"
allowed_origin = "https://baljinder22g.github.io"

# Document Intelligence: F0 (free, limited) or S0 (standard / pay-as-you-go).
docint_sku = "F0"

# Set to false if your subscription does NOT yet have Azure OpenAI access.
# Request access at https://aka.ms/oai/access first, then set true.
deploy_openai = true

# api_key = "testcreatedon31may"
