output "registry_id" {
  value     = yandex_container_registry.project.id
  sensitive = true
}

output "probe_secret_id" {
  value     = yandex_lockbox_secret.probe.id
  sensitive = true
}

output "probe_url" {
  value     = yandex_serverless_container.probe.url
  sensitive = true
}

output "application_image" {
  value = var.application_image
}

output "deployment_selection" {
  value = {
    vps_image            = var.application_image
    cloud_image          = "cr.yandex/${yandex_container_registry.project.id}/application@${split("@", var.application_image)[1]}"
    active_cloud_runtime = "infrastructure-probe"
  }
  sensitive = true
}

output "log_group_id" {
  value     = yandex_logging_group.runtime.id
  sensitive = true
}

output "container_id" {
  value     = yandex_serverless_container.probe.id
  sensitive = true
}

output "revision_id" {
  value     = yandex_serverless_container.probe.revision_id
  sensitive = true
}
