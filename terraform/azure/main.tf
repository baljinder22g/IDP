###############################################################################
# IDP Studio — Azure backend
# Creates: Resource Group, Storage (Function + log blobs), Linux Consumption
# Function App, Application Insights, Document Intelligence, Azure OpenAI.
# Implements api/openapi.yaml for: /v1/extract/azure and /v1/logs.
###############################################################################
terraform {
  required_version = ">= 1.5.0"
  required_providers {
    azurerm = { source = "hashicorp/azurerm", version = "~> 3.110" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "azurerm" {
  features {}
}

resource "random_string" "suffix" {
  length  = 5
  special = false
  upper   = false
}

locals {
  name   = "${var.project}${random_string.suffix.result}"
  log_container = "idp-logs"
}

resource "azurerm_resource_group" "rg" {
  name     = "${var.project}-rg"
  location = var.location
}

###############################################################################
# Storage (Functions runtime + log blobs)
###############################################################################
resource "azurerm_storage_account" "sa" {
  name                     = substr("${local.name}sa", 0, 24)
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"
}

resource "azurerm_storage_container" "logs" {
  name                  = local.log_container
  storage_account_name  = azurerm_storage_account.sa.name
  container_access_type = "private"
}

###############################################################################
# Application Insights
###############################################################################
resource "azurerm_application_insights" "ai" {
  name                = "${local.name}-ai"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  application_type    = "web"
  # Azure auto-links a Log Analytics workspace to new App Insights; don't fight it.
  lifecycle {
    ignore_changes = [workspace_id]
  }
}

###############################################################################
# Document Intelligence (Form Recognizer)
###############################################################################
resource "azurerm_cognitive_account" "docint" {
  name                  = "${local.name}-docint"
  location              = azurerm_resource_group.rg.location
  resource_group_name   = azurerm_resource_group.rg.name
  kind                  = "FormRecognizer"
  sku_name              = var.docint_sku
  custom_subdomain_name = "${local.name}-docint"
}

###############################################################################
# Azure OpenAI (optional — requires subscription access)
###############################################################################
resource "azurerm_cognitive_account" "openai" {
  count                 = var.deploy_openai ? 1 : 0
  name                  = "${local.name}-openai"
  location              = azurerm_resource_group.rg.location
  resource_group_name   = azurerm_resource_group.rg.name
  kind                  = "OpenAI"
  sku_name              = "S0"
  custom_subdomain_name = "${local.name}-openai"
}

resource "azurerm_cognitive_deployment" "mapping" {
  count                = var.deploy_openai ? 1 : 0
  name                 = "gpt-4o-mapping"
  cognitive_account_id = azurerm_cognitive_account.openai[0].id
  model {
    format  = "OpenAI"
    name    = var.openai_model
    version = var.openai_model_version
  }
  scale {
    type     = "Standard"
    capacity = 10
  }
}

###############################################################################
# Function App (Linux) — compute lives in var.functions_location, which may
# differ from var.location to dodge the new-subscription App Service quota wall.
###############################################################################
resource "azurerm_service_plan" "plan" {
  name                = "${local.name}-plan"
  resource_group_name = azurerm_resource_group.rg.name
  location            = var.functions_location
  os_type             = "Linux"
  sku_name            = var.functions_plan_sku
}

resource "azurerm_linux_function_app" "fn" {
  name                       = "${local.name}-fn"
  resource_group_name        = azurerm_resource_group.rg.name
  location                   = var.functions_location
  service_plan_id            = azurerm_service_plan.plan.id
  storage_account_name       = azurerm_storage_account.sa.name
  storage_account_access_key = azurerm_storage_account.sa.primary_access_key

  site_config {
    # always_on must be false on the Y1 Consumption plan, true on dedicated (B1+).
    always_on = var.functions_plan_sku != "Y1"
    application_stack { python_version = "3.11" }
    cors {
      allowed_origins = [var.allowed_origin]
    }
  }

  app_settings = merge({
    "FUNCTIONS_WORKER_RUNTIME" = "python"
    "LOG_CONTAINER"            = local.log_container
    "ALLOWED_ORIGIN"           = var.allowed_origin
    "API_KEY"                  = var.api_key
    "DOCINT_ENDPOINT"          = azurerm_cognitive_account.docint.endpoint
    "DOCINT_KEY"               = azurerm_cognitive_account.docint.primary_access_key
    "APPINSIGHTS_INSTRUMENTATIONKEY" = azurerm_application_insights.ai.instrumentation_key
    }, var.deploy_openai ? {
    "AOAI_ENDPOINT" = azurerm_cognitive_account.openai[0].endpoint
    "AOAI_KEY"      = azurerm_cognitive_account.openai[0].primary_access_key
  } : {})
}
