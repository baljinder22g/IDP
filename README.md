# IDP Studio 📄

A single-page **Intelligent Document Processing** app for insurance broker /
underwriting documents. Upload a PDF + a **Target JSON** schema, and get the
document extracted into that schema using four interchangeable strategies:

1. **AWS Bedrock** — PDF straight to a foundation model.
2. **AWS Textract** — OCR/forms, then map to schema.
3. **Agentic workflow** — Ingest → OCR → PII-mask → Extract → Validate, each agent shown in the UI.
4. **Azure Document Intelligence** — the same flow on the Azure stack.

Plus a **Logs** tab (backed by S3 / Blob) and a **Documentation** tab with live
architecture diagrams. Dark/light theme included.

> 🟢 **Works immediately with zero cloud setup.** With no API URL configured the
> app runs in **Mock mode** — perfect for a GitHub Pages demo. Add your deployed
> API URL in Settings (⚙️) to switch to the real AWS/Azure backend.

## Repository layout

```
.
├── index.html              # the app
├── assets/css|js/          # styles + logic (theme, tabs, API client, mock)
├── api/openapi.yaml        # the single API contract (AWS + Azure implement it)
├── terraform/aws/          # AWS backend: S3, IAM, 4 Lambdas, HTTP API Gateway
│   └── lambda/             # Python: bedrock, textract, agents, logs
├── terraform/azure/        # Azure backend: Function App, Doc Intelligence, OpenAI
│   └── function/           # Python Azure Functions
├── docs/documentation.md   # purpose + HLD/LLD diagrams (rendered in the Docs tab)
└── DEPLOYMENT.md           # step-by-step for brand-new free-tier accounts
```

## Quick start

### A) Host the UI on GitHub Pages (no backend needed)
1. Push this repo to GitHub.
2. **Settings → Pages → Build and deployment → Source: Deploy from a branch**, pick `main` / root.
3. Open `https://<you>.github.io/<repo>/` — it runs in Mock mode.

### B) Run locally
```bash
python -m http.server 8080
# open http://localhost:8080
```
(Use a server, not file://, so the Docs tab can fetch the markdown.)

### C) Connect a real backend
Deploy AWS and/or Azure (see **[DEPLOYMENT.md](DEPLOYMENT.md)**), then in the app
click ⚙️ and paste the `api_base_url` Terraform output. Done.

## Which cloud does what
- **AWS** serves the Bedrock, Textract, Agents and Logs tabs.
- **Azure** serves the Azure tab (Document Intelligence + Azure OpenAI) and its own Logs.
- The frontend is identical either way — it just targets one API base URL.

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for the full walkthrough, and
**[docs/documentation.md](docs/documentation.md)** for architecture.
