locals {
  name = "zont-${var.environment}"
}

resource "yandex_container_registry" "project" {
  folder_id = data.yandex_resourcemanager_folder.project.id
  name      = "${local.name}-images"
}

resource "yandex_container_registry_iam_binding" "pull" {
  registry_id = yandex_container_registry.project.id
  role        = "container-registry.images.puller"
  members     = ["serviceAccount:${var.runtime_service_account_id}"]
}

resource "yandex_logging_group" "runtime" {
  folder_id        = data.yandex_resourcemanager_folder.project.id
  name             = "${local.name}-runtime"
  retention_period = "168h"
}

resource "yandex_lockbox_secret" "probe" {
  folder_id = data.yandex_resourcemanager_folder.project.id
  name      = "${local.name}-probe"
}

resource "yandex_lockbox_secret_iam_binding" "runtime" {
  secret_id = yandex_lockbox_secret.probe.id
  role      = "lockbox.payloadViewer"
  members   = ["serviceAccount:${var.runtime_service_account_id}"]
}

resource "yandex_ydb_database_serverless" "probe" {
  folder_id           = data.yandex_resourcemanager_folder.project.id
  name                = "${local.name}-isolated"
  deletion_protection = true
  serverless_database {
    enable_throttling_rcu_limit = true
    provisioned_rcu_limit       = 0
    throttling_rcu_limit        = 10
    storage_size_limit          = 1
  }
}

resource "yandex_storage_bucket" "publication" {
  folder_id     = data.yandex_resourcemanager_folder.project.id
  bucket        = var.publication_bucket_name
  max_size      = 1073741824
  force_destroy = false
  anonymous_access_flags {
    read        = false
    list        = false
    config_read = false
  }
  versioning {
    enabled = true
  }
}

resource "yandex_serverless_container" "probe" {
  folder_id          = data.yandex_resourcemanager_folder.project.id
  name               = "${local.name}-probe"
  memory             = 256
  cores              = 1
  core_fraction      = 100
  concurrency        = 1
  execution_timeout  = "30s"
  service_account_id = var.runtime_service_account_id
  runtime {
    type = "http"
  }
  image {
    url    = var.probe_image
    digest = split("@", var.probe_image)[1]
    environment = {
      ZONT_ENVIRONMENT = var.environment
      ZONT_PROBE_ONLY  = "true"
      ZONT_FOLDER_ID   = var.folder_id
      PROBE_SMOKE_URL  = var.smoke_url
    }
  }
  secrets {
    id                   = yandex_lockbox_secret.probe.id
    version_id           = var.secret_version_id
    key                  = "xray_config"
    environment_variable = "XRAY_CONFIG"
  }
  secrets {
    id                   = yandex_lockbox_secret.probe.id
    version_id           = var.secret_version_id
    key                  = "web_credentials"
    environment_variable = "PROBE_WEB_CREDENTIALS"
  }
  dynamic "secrets" {
    for_each = var.grafana_metrics_enabled ? [true] : []
    content {
      id                   = yandex_lockbox_secret.probe.id
      version_id           = var.secret_version_id
      key                  = "grafana_otlp_config"
      environment_variable = "GRAFANA_OTLP_CONFIG"
    }
  }
  mounts {
    mount_point_path = "/publication"
    mode             = "ro"
    object_storage {
      bucket = yandex_storage_bucket.publication.bucket
      prefix = "m1/"
    }
  }
  log_options {
    log_group_id = yandex_logging_group.runtime.id
    min_level    = "INFO"
  }
  depends_on = [
    yandex_container_registry_iam_binding.pull,
    yandex_lockbox_secret_iam_binding.runtime,
    yandex_storage_bucket_iam_binding.probe,
  ]
}

resource "yandex_storage_bucket_iam_binding" "probe" {
  bucket  = yandex_storage_bucket.publication.bucket
  role    = "storage.viewer"
  members = ["serviceAccount:${var.runtime_service_account_id}"]
}

resource "yandex_storage_object" "probe" {
  bucket       = yandex_storage_bucket.publication.bucket
  key          = "m1/probe.txt"
  content      = "m1-private-object\n"
  content_type = "text/plain"
}

resource "yandex_serverless_container_iam_binding" "timer" {
  container_id = yandex_serverless_container.probe.id
  role         = "serverless.containers.invoker"
  members      = ["serviceAccount:${var.timer_service_account_id}"]
}

resource "yandex_function_trigger" "timer" {
  count     = var.enable_timer ? 1 : 0
  folder_id = data.yandex_resourcemanager_folder.project.id
  name      = "${local.name}-probe-timer"
  timer {
    cron_expression = var.timer_schedule
  }
  container {
    id                 = yandex_serverless_container.probe.id
    service_account_id = var.timer_service_account_id
    path               = "/smoke"
    retry_attempts     = 1
    retry_interval     = 10
  }
  depends_on = [yandex_serverless_container_iam_binding.timer]
}
