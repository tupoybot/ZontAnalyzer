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
