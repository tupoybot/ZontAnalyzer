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
  application_image          = "ghcr.io/example/application@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
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
    application_image = "ghcr.io/example/application:latest"
  }
  expect_failures = [var.application_image]
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
