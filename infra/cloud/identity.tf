locals {
  site_shell        = replace(file("${path.module}/../static/site.html"), "__ZONT_OIDC_CONFIG__", jsonencode(var.identity == null ? {} : { clientId = var.identity.client_id, issuer = var.identity.issuer }))
  identity_security = [{ identityHub = [] }]
  identity_paths = jsondecode(var.identity == null ? "{}" : jsonencode(merge(
    {
      for path in ["/", "/index.html", "/latest.html", "/za/", "/za/index.html", "/za/latest.html", "/auth/login", "/auth/callback", "/auth/logout", "/login"] : path => {
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
            object             = "${var.application_publication_prefix}/site-index.json"
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
            object             = "${var.application_publication_prefix}/publication/{path}"
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
    }
  )))
}
