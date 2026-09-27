resource "yandex_function" "auth" {
  count              = var.identity != null ? 1 : 0
  folder_id          = data.yandex_resourcemanager_folder.project.id
  name               = "${local.name}-auth"
  runtime            = "nodejs22"
  entrypoint         = "index.handler"
  memory             = 128
  execution_timeout  = "15"
  concurrency        = 4
  service_account_id = var.identity.auth_service_account
  user_hash          = var.identity.code_sha256
  content { zip_filename = "${path.module}/../auth/auth.zip" }
  environment = {
    OIDC_PUBLIC_ORIGIN = "https://${var.test_domain}"
    OIDC_CLIENT_ID     = var.identity.client_id
    OIDC_ISSUER        = var.identity.issuer
  }
  secrets {
    id                   = var.identity.client_secret_id
    version_id           = var.identity.client_secret_version
    key                  = "client_secret"
    environment_variable = "OIDC_CLIENT_SECRET"
  }
  secrets {
    id                   = var.identity.transaction_secret_id
    version_id           = var.identity.transaction_version
    key                  = "transaction_key"
    environment_variable = "OIDC_TRANSACTION_KEY"
  }
  log_options {
    log_group_id = yandex_logging_group.runtime.id
    min_level    = "WARN"
  }
}
resource "yandex_function_iam_binding" "auth" {
  count       = var.identity != null ? 1 : 0
  function_id = yandex_function.auth[0].id
  role        = "functions.functionInvoker"
  members     = ["serviceAccount:${var.timer_service_account_id}"]
}

locals {
  site_shell        = file("${path.module}/../static/site.html")
  identity_security = [{ identityHub = [] }]
  identity_paths = jsondecode(var.identity == null ? "{}" : jsonencode(merge(
    {
      for path in ["/", "/index.html", "/latest.html", "/za/", "/za/index.html", "/za/latest.html"] : path => {
        get = {
          responses = { "200" = { description = "Empty report resolver" } }
          "x-yc-apigateway-integration" = {
            type         = "dummy"
            http_code    = 200
            http_headers = { "Content-Type" = "text/html; charset=utf-8", "Cache-Control" = "no-store", "Referrer-Policy" = "no-referrer" }
            content      = { "*" = local.site_shell }
          }
        }
      }
    },
    {
      for path in ["/daily/{file}", "/weekly/{file}", "/monthly/{file}", "/seasonal/{file}", "/za/daily/{file}", "/za/weekly/{file}", "/za/monthly/{file}", "/za/seasonal/{file}"] : path => {
        parameters = [{ name = "file", in = "path", required = true, schema = { type = "string" } }]
        get = {
          responses = { "200" = { description = "Empty archive resolver" } }
          "x-yc-apigateway-integration" = {
            type         = "dummy"
            http_code    = 200
            http_headers = { "Content-Type" = "text/html; charset=utf-8", "Cache-Control" = "no-store", "Referrer-Policy" = "no-referrer" }
            content      = { "*" = local.site_shell }
          }
        }
      }
    },
    {
      for path in ["/reports.json", "/za/reports.json"] : path => {
        get = {
          security  = local.identity_security
          responses = { "200" = { description = "Committed private report index" } }
          "x-yc-apigateway-integration" = {
            type               = "object_storage"
            bucket             = yandex_storage_bucket.publication.bucket
            object             = "reports/site-index.json"
            service_account_id = var.timer_service_account_id
          }
        }
      }
    },
    {
      "/objects/publication/{path+}" = {
        parameters = [{ name = "path", in = "path", required = true, schema = { type = "string" } }]
        get = {
          security  = local.identity_security
          responses = { "200" = { description = "Private immutable report object" } }
          "x-yc-apigateway-integration" = {
            type               = "object_storage"
            bucket             = yandex_storage_bucket.publication.bucket
            object             = "reports/publication/{path}"
            service_account_id = var.timer_service_account_id
          }
        }
      }
    },
    {
      for path in ["/api/{path+}", "/za/api/{path+}"] : path => merge(
        { parameters = [{ name = "path", in = "path", required = true, schema = { type = "string" } }] },
        { for method in ["get", "put", "post"] : method => {
          security  = local.identity_security
          responses = { "200" = { description = "Authenticated application action" } }
          "x-yc-apigateway-integration" = {
            type               = "serverless_containers"
            container_id       = yandex_serverless_container.application.id
            service_account_id = var.timer_service_account_id
          }
        } }
      )
    },
    {
      for route in ["/daily/{date}.json", "/weekly/{date}.json", "/monthly/{date}.json", "/seasonal/{date}.json", "/za/daily/{date}.json", "/za/weekly/{date}.json", "/za/monthly/{date}.json", "/za/seasonal/{date}.json"] : route => {
        parameters = [{ name = "date", in = "path", required = true, schema = { type = "string" } }]
        get = {
          security  = local.identity_security
          responses = { "200" = { description = "Canonical JSON export by logical date" } }
          "x-yc-apigateway-integration" = {
            type               = "serverless_containers"
            container_id       = yandex_serverless_container.application.id
            service_account_id = var.timer_service_account_id
          }
        }
      }
    },
    {
      for path in ["/auth/login", "/auth/callback", "/auth/logout"] : path => {
        (path == "/auth/logout" ? "post" : "get") = {
          responses = { "303" = { description = "OIDC redirect or callback" } }
          "x-yc-apigateway-integration" = {
            type               = "cloud_functions"
            function_id        = yandex_function.auth[0].id
            service_account_id = var.timer_service_account_id
          }
        }
      }
    },
    {
      "/login" = {
        get = {
          responses = { "303" = { description = "Identity Hub login" } }
          "x-yc-apigateway-integration" = {
            type         = "dummy"
            http_code    = 303
            http_headers = { Location = "/auth/login", "Cache-Control" = "no-store" }
            content      = { "*" = "" }
          }
        }
      }
    }
  )))
}
