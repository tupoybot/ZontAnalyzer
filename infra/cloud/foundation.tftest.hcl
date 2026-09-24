mock_provider "yandex" {}

override_data {
  target = data.yandex_resourcemanager_folder.project
  values = {
    id       = "test-folder"
    cloud_id = "test-cloud"
  }
}

variables {
  cloud_id                   = "test-cloud"
  folder_id                  = "test-folder"
  runtime_service_account_id = "test-runtime"
  timer_service_account_id   = "test-timer"
  smoke_url                  = "https://synthetic.example/"
  test_domain                = "test.example"
  environment                = "dev"
  publication_bucket_name    = "test-publication"
  probe_image                = "cr.yandex/test/probe@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  application_image          = "cr.yandex/test/application@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  application_revision       = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  application_ydb_namespace  = "test_application"
  openai_smoke_model         = "gpt-5.2"
  secret_version_id          = "test-version"
}

run "reject_wrong_cloud" {
  command = plan
  override_data {
    target = data.yandex_resourcemanager_folder.project
    values = {
      id       = "test-folder"
      cloud_id = "other-cloud"
    }
  }
  expect_failures = [data.yandex_resourcemanager_folder.project]
}

run "isolated_defaults" {
  command = plan

  assert {
    condition     = yandex_ydb_database_serverless.probe.deletion_protection && yandex_cm_certificate.probe.deletion_protection
    error_message = "Persistent resources must be protected by default."
  }

  assert {
    condition     = length(yandex_function_trigger.timer) == 0
    error_message = "A new environment must not start its timer."
  }
  assert {
    condition     = !one(yandex_storage_bucket.publication.anonymous_access_flags).read && !one(yandex_storage_bucket.publication.anonymous_access_flags).list
    error_message = "Publication must not be anonymously readable or listable."
  }
  assert {
    condition     = yandex_serverless_container.probe.concurrency == 1 && yandex_serverless_container.probe.execution_timeout == "30s"
    error_message = "The initial probe must have bounded invocation resources."
  }
  assert {
    condition     = yandex_serverless_container.application.memory == 512 && yandex_serverless_container.application.cores == 1 && yandex_serverless_container.application.core_fraction == 100 && yandex_serverless_container.application.concurrency == 1 && yandex_serverless_container.application.execution_timeout == "210s"
    error_message = "The application must have the bounded report runtime budget."
  }
  assert {
    condition     = length(yandex_serverless_container.application.mounts) == 0
    error_message = "M2 application must not mount storage or SQLite state."
  }
  assert {
    condition     = !var.openai_access_confirmed && yandex_serverless_container.application.image[0].environment.CLOUD_OPENAI_ACCESS_CONFIRMED == "false" && yandex_serverless_container.application.image[0].environment.CLOUD_JOB_TIMEOUT_SECONDS == "15"
    error_message = "OpenAI access must remain disabled by default and jobs must be bounded."
  }
  assert {
    condition     = length([for trigger in yandex_function_trigger.timer : trigger if trigger.container[0].id == yandex_serverless_container.application.id]) == 0
    error_message = "M2 application must not have a scheduler."
  }
  assert {
    condition     = alltrue([for expected in ["xray_config", "web_credentials", "zont_token", "zont_client_email", "openai_api_key"] : contains([for secret in yandex_serverless_container.application.secrets : secret.key], expected)])
    error_message = "The application must receive all required Lockbox references."
  }
}

run "reject_floating_probe" {
  command = plan
  variables {
    probe_image = "cr.yandex/test/probe:latest"
  }
  expect_failures = [var.probe_image]
}

run "reject_floating_application" {
  command = plan
  variables {
    application_image = "cr.yandex/test/application:latest"
  }
  expect_failures = [var.application_image]
}

run "reject_non_registry_application" {
  command = plan
  variables {
    application_image = "ghcr.io/example/application@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  }
  expect_failures = [var.application_image]
}

run "reject_short_application_revision" {
  command = plan
  variables {
    application_revision = "bbbbbbbb"
  }
  expect_failures = [var.application_revision]
}

run "gateway_routes_use_the_correct_container" {
  command = plan

  override_resource {
    target          = yandex_serverless_container.probe
    override_during = plan
    values = {
      id = "probe-container"
    }
  }

  override_resource {
    target          = yandex_serverless_container.application
    override_during = plan
    values = {
      id = "application-container"
    }
  }

  assert {
    condition     = alltrue([for path in ["/api/probe", "/private/probe.txt"] : yamldecode(yandex_api_gateway.probe.spec).paths[path]["get"]["x-yc-apigateway-integration"].container_id == "probe-container"])
    error_message = "Existing probe routes must continue to invoke the probe container."
  }
  assert {
    condition     = alltrue([for path in ["/ready", "/diagnostics"] : yamldecode(yandex_api_gateway.probe.spec).paths[path]["get"]["x-yc-apigateway-integration"].container_id == "application-container"])
    error_message = "Application readiness and diagnostics routes must invoke the application container."
  }
  assert {
    condition     = alltrue([for path in ["/jobs/analytics", "/jobs/integrations", "/jobs/reports"] : yamldecode(yandex_api_gateway.probe.spec).paths[path]["post"]["x-yc-apigateway-integration"].container_id == "application-container"])
    error_message = "Application job routes must invoke the application container."
  }
  assert {
    condition     = yandex_api_gateway.probe.execution_timeout == "210" && yandex_serverless_container.application.image[0].environment.CLOUD_REPORT_TIMEOUT_SECONDS == "180"
    error_message = "The gateway and container must allow the bounded report job to finish."
  }
}

run "reject_invalid_application_namespace" {
  command = plan
  variables {
    application_ydb_namespace = "../other"
  }
  expect_failures = [var.application_ydb_namespace]
}

run "reject_production" {
  command = plan
  variables {
    environment = "prod"
  }
  expect_failures = [var.environment]
}

run "timer_requires_metrics" {
  command = plan
  variables {
    enable_timer = true
  }
  expect_failures = [var.enable_timer]
}

run "monitored_timer" {
  command = plan
  variables {
    enable_timer            = true
    grafana_metrics_enabled = true
  }
  assert {
    condition     = length(yandex_function_trigger.timer) == 1
    error_message = "A monitored environment must be able to enable its timer."
  }
  assert {
    condition     = anytrue([for secret in yandex_serverless_container.probe.secrets : secret.key == "grafana_otlp_config" && secret.environment_variable == "GRAFANA_OTLP_CONFIG"])
    error_message = "The runtime must receive Grafana credentials by secret reference."
  }
  assert {
    condition     = anytrue([for secret in yandex_serverless_container.application.secrets : secret.key == "grafana_otlp_config" && secret.environment_variable == "GRAFANA_OTLP_CONFIG"])
    error_message = "The application must receive Grafana credentials by secret reference."
  }
}

run "explicit_test_teardown" {
  command = plan
  variables {
    environment         = "pilot"
    deletion_protection = false
  }
  assert {
    condition     = !yandex_ydb_database_serverless.probe.deletion_protection && !yandex_cm_certificate.probe.deletion_protection
    error_message = "An explicitly selected test stack must support controlled teardown."
  }
}
