resource "yandex_serverless_container" "application" {
  folder_id          = data.yandex_resourcemanager_folder.project.id
  name               = "${local.name}-application"
  memory             = 512
  cores              = 1
  core_fraction      = 100
  concurrency        = 1
  execution_timeout  = "210s"
  service_account_id = var.runtime_service_account_id

  runtime {
    type = "http"
  }

  image {
    url    = var.application_image
    digest = split("@", var.application_image)[1]
    environment = {
      CLOUD_ENVIRONMENT             = var.environment
      CLOUD_REVISION                = var.application_revision
      CLOUD_JOB_TIMEOUT_SECONDS     = "15"
      CLOUD_REPORT_TIMEOUT_SECONDS  = "180"
      CLOUD_OPENAI_MODEL            = var.openai_smoke_model
      CLOUD_OPENAI_ACCESS_CONFIRMED = var.openai_access_confirmed ? "true" : "false"
      YDB_ENDPOINT                  = "grpcs://${yandex_ydb_database_serverless.probe.ydb_api_endpoint}"
      YDB_DATABASE                  = yandex_ydb_database_serverless.probe.database_path
      YDB_NAMESPACE                 = var.application_ydb_namespace
      YDB_METADATA_CREDENTIALS      = "1"
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
    environment_variable = "CLOUD_WEB_CREDENTIALS"
  }

  secrets {
    id                   = yandex_lockbox_secret.probe.id
    version_id           = var.secret_version_id
    key                  = "zont_token"
    environment_variable = "ZONT_TOKEN"
  }

  secrets {
    id                   = yandex_lockbox_secret.probe.id
    version_id           = var.secret_version_id
    key                  = "zont_client_email"
    environment_variable = "ZONT_CLIENT_EMAIL"
  }

  secrets {
    id                   = yandex_lockbox_secret.probe.id
    version_id           = var.secret_version_id
    key                  = "openai_api_key"
    environment_variable = "OPENAI_API_KEY"
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

  log_options {
    log_group_id = yandex_logging_group.runtime.id
    min_level    = "INFO"
  }

  depends_on = [
    yandex_container_registry_iam_binding.pull,
    yandex_lockbox_secret_iam_binding.runtime,
    yandex_ydb_database_iam_binding.application,
  ]
}

resource "yandex_ydb_database_iam_binding" "application" {
  database_id = yandex_ydb_database_serverless.probe.id
  role        = "ydb.editor"
  members     = ["serviceAccount:${var.runtime_service_account_id}"]
}

resource "yandex_serverless_container_iam_binding" "application_invoker" {
  container_id = yandex_serverless_container.application.id
  role         = "serverless.containers.invoker"
  members      = ["serviceAccount:${var.timer_service_account_id}"]
}
