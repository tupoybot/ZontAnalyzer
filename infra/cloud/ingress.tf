resource "yandex_cm_certificate" "probe" {
  folder_id           = data.yandex_resourcemanager_folder.project.id
  name                = "${local.name}-web"
  domains             = [var.test_domain]
  deletion_protection = true
  managed {
    challenge_type = "DNS_CNAME"
  }
}

resource "yandex_api_gateway" "probe" {
  folder_id = data.yandex_resourcemanager_folder.project.id
  name      = "${local.name}-web"
  spec = yamlencode({
    openapi = "3.0.0"
    info    = { title = "Isolated M1 probe", version = "1.0" }
    paths = {
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
    }
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
  depends_on = [yandex_serverless_container_iam_binding.timer]
}

output "certificate_challenges" {
  value     = yandex_cm_certificate.probe.challenges
  sensitive = true
}

output "gateway_domain" {
  value     = yandex_api_gateway.probe.domain
  sensitive = true
}
