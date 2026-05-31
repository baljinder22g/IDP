# Copy to terraform.tfvars and edit. All values are optional (defaults shown).

# AWS region where Bedrock + Textract are enabled.
region = "us-east-1"

# Restrict CORS to your GitHub Pages site (recommended for production).
# allowed_origin = "https://baljinder22g.github.io"
allowed_origin = "*"

# Default Bedrock model. Haiku 4.5 is confirmed working on this account.
# (Sonnet 4.5 needs an AWS Marketplace subscription that can't complete until a
#  payment method is added — switch back to Sonnet once billing is set up.)
bedrock_model = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Cost guardrail: monthly budget with email alerts at 50/80/100%.
monthly_budget_usd = 10
budget_alert_email = "baljinder.saluja@gmail.com"

# Optional shared secret. If set, the SPA must send the same value as x-api-key.

