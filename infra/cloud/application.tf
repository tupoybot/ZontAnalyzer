resource "yandex_serverless_container" "application" {
  folder_id          = data.yandex_resourcemanager_folder.project.id
  name               = "${local.name}-application"
  memory             = 512
  cores              = 1
  core_fraction      = 100
  concurrency        = 2
  execution_timeout  = "600s"
  service_account_id = var.runtime_service_account_id

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  runtime {
    type = "http"
  }

  image {
    url    = var.application_image
    digest = split("@", var.application_image)[1]
    environment = merge({
      CLOUD_ENVIRONMENT             = var.environment
      CLOUD_REVISION                = var.application_revision
      CLOUD_WRITES_ENABLED          = tostring(var.application_writes_enabled)
      CLOUD_JOB_TIMEOUT_SECONDS     = "15"
      CLOUD_REPORT_TIMEOUT_SECONDS  = "570"
      CLOUD_OPENAI_MODEL            = var.openai_smoke_model
      CLOUD_OPENAI_ACCESS_CONFIRMED = var.openai_access_confirmed ? "true" : "false"
      CLOUD_PUBLICATION_BUCKET      = yandex_storage_bucket.publication.bucket
      CLOUD_PUBLICATION_PREFIX      = var.application_publication_prefix
      CLOUD_PUBLIC_ORIGIN           = var.attach_domain ? "https://${var.test_domain}" : ""
      YDB_ENDPOINT                  = "grpcs://${local.application_database.ydb_api_endpoint}"
      YDB_DATABASE                  = local.application_database.database_path
      YDB_NAMESPACE                 = var.application_ydb_namespace
      YDB_METADATA_CREDENTIALS      = "1"
      }, var.identity == null ? {} : {
      CLOUD_OIDC_ISSUER   = var.identity.issuer
      CLOUD_OIDC_AUDIENCE = var.identity.client_id
      CLOUD_OIDC_JWKS_URI = "https://auth.yandex.cloud/oauth/jwks/keys"
    })
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
    for_each = var.application_config_secret_enabled ? [true] : []
    content {
      id                   = yandex_lockbox_secret.probe.id
      version_id           = var.secret_version_id
      key                  = "application_config_json"
      environment_variable = "ZONT_ANALYZER_CONFIG_JSON"
    }
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
    yandex_storage_bucket_iam_binding.application_uploader,
  ]
}

resource "yandex_storage_bucket_iam_binding" "application_uploader" {
  bucket  = yandex_storage_bucket.publication.bucket
  role    = "storage.uploader"
  members = ["serviceAccount:${var.runtime_service_account_id}"]
}

resource "yandex_ydb_database_iam_binding" "application" {
  database_id = local.application_database.id
  role        = "ydb.editor"
  members     = ["serviceAccount:${var.runtime_service_account_id}"]
}

resource "yandex_serverless_container_iam_binding" "application_invoker" {
  container_id = yandex_serverless_container.application.id
  role         = "serverless.containers.invoker"
  members      = ["serviceAccount:${var.timer_service_account_id}"]
}
