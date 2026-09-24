resource "yandex_cm_certificate" "probe" {
  folder_id           = data.yandex_resourcemanager_folder.project.id
  name                = "${local.name}-web"
  domains             = [var.test_domain]
  deletion_protection = var.deletion_protection
  managed {
    challenge_type = "DNS_CNAME"
  }
}

resource "yandex_api_gateway" "probe" {
  folder_id         = data.yandex_resourcemanager_folder.project.id
  name              = "${local.name}-web"
  execution_timeout = "210"
  spec = yamlencode({
    openapi = "3.0.0"
    info    = { title = "Isolated probe and YDB application", version = "4.0" }
    paths = merge(
      {
        for path in ["/api/probe", "/private/probe.txt"] : path => {
          get = {
            responses = { "200" = { description = "Authenticated probe" } }
            "x-yc-apigateway-integration" = {
              type               = "serverless_containers"
              container_id       = yandex_serverless_container.probe.id
              service_account_id = var.timer_service_account_id
            }
          }
        }
      },
      {
        for path in ["/ready", "/diagnostics"] : path => {
          get = {
            responses = { "200" = { description = "Application readiness or diagnostics" } }
            "x-yc-apigateway-integration" = {
              type               = "serverless_containers"
              container_id       = yandex_serverless_container.application.id
              service_account_id = var.timer_service_account_id
            }
          }
        }
      },
      {
        for path in ["/jobs/analytics", "/jobs/integrations", "/jobs/reports"] : path => {
          post = {
            responses = { "200" = { description = "Bounded application job completed" } }
            "x-yc-apigateway-integration" = {
              type               = "serverless_containers"
              container_id       = yandex_serverless_container.application.id
              service_account_id = var.timer_service_account_id
            }
          }
        }
      },
    )
  })
  dynamic "custom_domains" {
    for_each = var.attach_domain ? [true] : []
    content {
      fqdn           = var.test_domain
      certificate_id = yandex_cm_certificate.probe.id
    }
  }
  log_options {
    log_group_id = yandex_logging_group.runtime.id
    min_level    = "WARN"
  }
  depends_on = [
    yandex_serverless_container_iam_binding.timer,
    yandex_serverless_container_iam_binding.application_invoker,
  ]
}

output "certificate_challenges" {
  value     = yandex_cm_certificate.probe.challenges
  sensitive = true
}

output "gateway_domain" {
  value     = yandex_api_gateway.probe.domain
  sensitive = true
}
