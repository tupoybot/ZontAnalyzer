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

output "application_revision" {
  value = var.application_revision
}

output "deployment_selection" {
  value = {
    probe_image       = var.probe_image
    application_image = var.application_image
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

output "application_url" {
  value     = yandex_serverless_container.application.url
  sensitive = true
}

output "application_container_id" {
  value     = yandex_serverless_container.application.id
  sensitive = true
}

output "application_revision_id" {
  value     = yandex_serverless_container.application.revision_id
  sensitive = true
}
