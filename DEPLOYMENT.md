# Deployment Guide (for brand-new free-tier accounts)

This guide assumes **no prior AWS/Azure experience**. You only need the AWS
deployment for tabs 1–3 + logs, and the Azure deployment for tab 4. Do whichever
you want — they are independent.

---

## 0. Install the tools (one time)

| Tool | Windows install | Verify |
|------|-----------------|--------|
| Terraform | `winget install Hashicorp.Terraform` | `terraform -version` |
| AWS CLI | `winget install Amazon.AWSCLI` | `aws --version` |
| Azure CLI | `winget install Microsoft.AzureCLI` | `az --version` |
| Azure Functions Core Tools | `winget install Microsoft.Azure.FunctionsCoreTools` | `func --version` |

Restart your terminal after installing so the PATH updates.

---

## PART A — AWS (Bedrock, Textract, Agents, Logs)

### A1. Create a free AWS account
1. Go to https://aws.amazon.com/free and sign up (needs a card; free tier won't charge for the amounts here).
2. Sign in to the **AWS Console** as the root user.

### A2. Create an access key for the CLI
1. Console → search **IAM** → **Users** → **Create user** (e.g. `terraform`).
2. Attach policy **AdministratorAccess** (simplest for setup; tighten later).
3. Open the user → **Security credentials** → **Create access key** → choose **CLI**.
4. Copy the **Access key ID** and **Secret access key**.

### A3. Configure the CLI
```powershell
aws configure
# AWS Access Key ID:     <paste>
# AWS Secret Access Key: <paste>
# Default region name:   us-east-1
# Default output format: json
```

### A4. Enable Bedrock model access (IMPORTANT — one click, easy to miss)
1. Console → **Bedrock** (make sure the region top-right is **us-east-1**).
2. Left menu → **Model access** → **Enable specific models** (or *Manage*).
3. Enable the **Anthropic Claude** models. Approval is usually instant.

> If `region` in your `terraform.tfvars` is not `us-east-1`, enable models in
> that region instead. Bedrock + Textract must both exist in that region.

> **Can't enable a model? (no payment method / Marketplace blocked)** New
> accounts sometimes can't complete the AWS Marketplace subscription that gates
> Bedrock models (error: `INVALID_PAYMENT_INSTRUMENT` or
> `aws-marketplace:Subscribe ... not authorized`). Two ways around it:
> 1. Add a valid card under **Billing → Payment preferences**, then retry.
> 2. **Use a Bedrock API key (bearer token):** Console → **Bedrock → API keys**
>    → generate a **long-term** key. Then either set `bedrock_api_key` in
>    `terraform.tfvars` (baked into the Lambda), **or** paste it per-session into
>    the app's ⚙️ Settings → *Bedrock API key* field. The backend then calls
>    Bedrock with the token instead of its IAM role. Short-term keys also work
>    but expire (~12h).
>
> Use a cross-region **inference-profile** model id (e.g.
> `us.anthropic.claude-haiku-4-5-20251001-v1:0` or a `global.` profile). Bare
> on-demand ids (`anthropic.claude-...`) return *"on-demand throughput isn't
> supported"*.

### A5. Deploy
```powershell
cd terraform\aws
copy terraform.tfvars.example terraform.tfvars   # then edit if you like
terraform init
terraform plan        # review what will be created
terraform apply       # type 'yes'
```
When it finishes, copy the **`api_base_url`** output.

### A6. Point the app at it
1. Open IDP Studio (GitHub Pages or local).
2. Click ⚙️ → paste `api_base_url` into **API base URL**.
3. If you set `api_key` in tfvars, paste the same value into **API key**.
4. (Optional) If you're using a Bedrock API key instead of IAM and didn't bake
   it into `terraform.tfvars`, paste it into **Bedrock API key**. Save.
5. The pill top-right turns green (**Live**). Upload a PDF and run.

### A7. Tear down (stop all costs)
```powershell
terraform destroy   # type 'yes'
```

---

## PART B — Azure (Document Intelligence + Azure OpenAI)

### B1. Create a free Azure account
https://azure.microsoft.com/free → sign up.

### B2. (If using Azure OpenAI) request access
Azure OpenAI requires a quick access request: https://aka.ms/oai/access.
**If not yet approved**, set `deploy_openai = false` in `terraform.tfvars` and
deploy now; the Document Intelligence step still works, and you can enable
OpenAI later by flipping it back to `true` and re-running `apply`.

### B3. Log in
```powershell
az login            # opens a browser
az account show     # confirm the right subscription
```

### B4. Deploy infrastructure
```powershell
cd terraform\azure
copy terraform.tfvars.example terraform.tfvars   # edit location/sku as needed
terraform init
terraform apply     # type 'yes'
```
Copy the outputs: **`function_app_name`** and **`api_base_url`**.

### B5. Publish the function code
```powershell
cd function
func azure functionapp publish <function_app_name>
```
(`<function_app_name>` is the Terraform output from B4.)

### B6. Point the app at it
1. ⚙️ → set **API base URL** to the `api_base_url` output
   (looks like `https://idpstudioXXXXX-fn.azurewebsites.net/api`).
2. Azure Functions protect routes with a **function key**. Either:
   - append `?code=<function-key>` to the base URL (get the key in
     Portal → Function App → **App keys**), **or**
   - set the same `api_key` value in tfvars and the app's API-key field.
3. Save, open **Tab ④ Azure**, upload a PDF, run.

### B7. Tear down
```powershell
terraform destroy   # type 'yes'
```

---

## Validate without deploying

`terraform validate` checks syntax/config without touching the cloud:
```powershell
cd terraform\aws   ; terraform init -backend=false ; terraform validate
cd ..\azure        ; terraform init -backend=false ; terraform validate
```

The Python Lambda/Function code compiles cleanly:
```powershell
python -m py_compile terraform\aws\lambda\handler.py terraform\aws\lambda\common.py
python -m py_compile terraform\azure\function\extract_azure\__init__.py terraform\azure\function\list_logs\__init__.py
```

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| App says "Mock mode" still | You didn't save an API base URL, or it's blank. |
| CORS error in browser console | Set `allowed_origin` to your exact GitHub Pages origin and `terraform apply` again. |
| Bedrock `AccessDenied` / model not found | Enable model access (A4) in the **same region** as `var.region`. |
| Textract slow / times out | Large multi-page PDFs take longer; the Lambda polls up to ~30s. Use smaller PDFs for the demo. |
| Azure 401 on calls | Missing function key — append `?code=<key>` or set the matching `api_key`. |
| Azure OpenAI deploy fails | Your subscription lacks OpenAI access; set `deploy_openai = false` and request access. |
