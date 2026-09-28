mock_provider "yandex" {}

run "production_database_is_separate_and_capped" {
  command = plan
  variables {
    enable_production_database              = true
    production_ydb_request_units_per_second = 1000
    application_writes_enabled              = false
  }
  override_resource {
    target          = yandex_ydb_database_serverless.production[0]
    override_during = plan
    values = {
      id               = "test-production-database"
      database_path    = "/test/production"
      ydb_api_endpoint = "production.example:2135"
    }
  }
  assert {
    condition     = yandex_ydb_database_serverless.production[0].deletion_protection && one(yandex_ydb_database_serverless.production[0].serverless_database).throttling_rcu_limit == 1000 && one(yandex_ydb_database_serverless.production[0].serverless_database).provisioned_rcu_limit == 0
    error_message = "Production must be protected, capped and billed on demand."
  }
  assert {
    condition     = yandex_serverless_container.application.image[0].environment.YDB_DATABASE == "/test/production" && yandex_serverless_container.application.image[0].environment.YDB_ENDPOINT == "grpcs://production.example:2135" && yandex_ydb_database_iam_binding.application.database_id == "test-production-database"
    error_message = "Application credentials and connection must select the same production database."
  }
  assert {
    condition     = yandex_ydb_database_serverless.probe.name == "zont-dev-isolated" && yandex_serverless_container.application.image[0].environment.CLOUD_WRITES_ENABLED == "false" && length(yandex_function_trigger.scheduler) == 0
    error_message = "Creating production must retain development data and permit a closed write gate."
  }
}

run "production_quota_increase_is_not_implicit" {
  command = plan
  variables {
    production_ydb_request_units_per_second = 50000
  }
  expect_failures = [var.production_ydb_request_units_per_second]
}

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
    condition     = var.identity == null && !contains(keys(yandex_serverless_container.application.image[0].environment), "CLOUD_OIDC_ISSUER")
    error_message = "Null identity must preserve the original isolated scope without an auth function or OIDC configuration."
  }

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
    condition     = yandex_serverless_container.application.memory == 512 && yandex_serverless_container.application.cores == 1 && yandex_serverless_container.application.core_fraction == 100 && yandex_serverless_container.application.concurrency == 2 && yandex_serverless_container.application.execution_timeout == "210s"
    error_message = "The application must have the bounded report runtime budget."
  }
  assert {
    condition     = length(yandex_serverless_container.application.mounts) == 0
    error_message = "M2 application must not mount storage or SQLite state."
  }
  assert {
    condition     = yandex_serverless_container.application.metadata_options[0].gce_http_endpoint == 1 && yandex_serverless_container.application.metadata_options[0].aws_v1_http_endpoint == 2
    error_message = "YDB credentials require the GCE metadata endpoint; AWS IMDSv1 must remain disabled."
  }
  assert {
    condition     = !var.openai_access_confirmed && yandex_serverless_container.application.image[0].environment.CLOUD_OPENAI_ACCESS_CONFIRMED == "false" && yandex_serverless_container.application.image[0].environment.CLOUD_JOB_TIMEOUT_SECONDS == "15"
    error_message = "OpenAI access must remain disabled by default and jobs must be bounded."
  }
  assert {
    condition     = yandex_serverless_container.application.image[0].environment.CLOUD_PUBLICATION_BUCKET == yandex_storage_bucket.publication.bucket && yandex_serverless_container.application.image[0].environment.CLOUD_PUBLICATION_PREFIX == "reports" && yandex_serverless_container.application.image[0].environment.CLOUD_PUBLIC_ORIGIN == (var.attach_domain ? "https://${var.test_domain}" : "")
    error_message = "The application must use the private publication bucket, reports prefix, and origin matching its attached domain configuration."
  }
  assert {
    condition     = length(yandex_function_trigger.scheduler) == 0 && !var.enable_scheduler_timer
    error_message = "A new environment must not start the application scheduler."
  }
  assert {
    condition     = yandex_serverless_container.application.image[0].environment.CLOUD_WRITES_ENABLED == "true" && !contains([for secret in yandex_serverless_container.application.secrets : secret.key], "application_config_json")
    error_message = "Compatibility defaults enable writes without requiring an application configuration secret."
  }
  assert {
    condition     = alltrue([for expected in ["xray_config", "web_credentials", "zont_token", "zont_client_email", "openai_api_key"] : contains([for secret in yandex_serverless_container.application.secrets : secret.key], expected)])
    error_message = "The application must receive all required Lockbox references."
  }
}

run "identity_protects_native_publication" {
  command = plan
  variables {
    attach_domain = true
    identity = {
      client_id = "test-oidc-client"
      issuer    = "https://auth.yandex.cloud"
      mode      = "spa"
    }
  }
  override_resource {
    target          = yandex_serverless_container.probe
    override_during = plan
    values          = { id = "test-probe" }
  }
  override_resource {
    target          = yandex_serverless_container.application
    override_during = plan
    values          = { id = "test-application" }
  }

  assert {
    condition = (
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].type == "jwt" &&
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].identitySource.in == "header" &&
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].identitySource.name == "Authorization" &&
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].identitySource.prefix == "Bearer " &&
      jsonencode(yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].issuers) == jsonencode([var.identity.issuer]) &&
      jsonencode(yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].audiences) == jsonencode([var.identity.client_id]) &&
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].jwksUri == "https://auth.yandex.cloud/oauth/jwks/keys" &&
      toset(yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].requiredClaims) == toset(["sub", "exp", "iat"])
    )
    error_message = "The gateway must verify the OIDC bearer token against exactly the configured issuer and audience."
  }
  assert {
    condition = alltrue([for path, object in {
      "/reports.json"                = "reports/site-index.json"
      "/za/reports.json"             = "reports/site-index.json"
      "/objects/publication/{path+}" = "reports/publication/{path}"
      } : (
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].type == "object_storage" &&
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].bucket == yandex_storage_bucket.publication.bucket &&
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].object == object &&
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].service_account_id == var.timer_service_account_id &&
      !contains(keys(yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"]), "container_id") &&
      jsonencode(yamldecode(yandex_api_gateway.probe.spec).paths[path].get.security) == jsonencode([{ identityHub = [] }])
    )])
    error_message = "Private report objects and the committed index must use JWT-protected native S3 delivery without invoking the app."
  }
  assert {
    condition = alltrue([for path in ["/", "/index.html", "/latest.html", "/za/", "/za/index.html", "/za/latest.html", "/daily/{file}", "/weekly/{file}", "/monthly/{file}", "/seasonal/{file}"] : (
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].type == "dummy" &&
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].content["*"] == local.site_shell
    )])
    error_message = "Report navigation must serve only the static resolver without app or YDB reads."
  }
  assert {
    condition = alltrue(flatten([for path in ["/api/{path+}", "/za/api/{path+}"] : [for method in ["get", "put", "post"] : (
      yamldecode(yandex_api_gateway.probe.spec).paths[path][method]["x-yc-apigateway-integration"].container_id == "test-application" &&
      jsonencode(yamldecode(yandex_api_gateway.probe.spec).paths[path][method].security) == jsonencode([{ identityHub = [] }])
    )]]))
    error_message = "Every application API method must carry the same JWT perimeter as private report content."
  }
  assert {
    condition = alltrue([for path in ["/auth/login", "/auth/callback", "/auth/logout", "/login"] : (
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].type == "dummy" &&
      yamldecode(yandex_api_gateway.probe.spec).paths[path].get["x-yc-apigateway-integration"].content["*"] == local.site_shell
    )])
    error_message = "Login and callback must execute solely in the browser."
  }
  assert {
    condition = (
      yandex_serverless_container.application.image[0].environment.CLOUD_OIDC_ISSUER == var.identity.issuer &&
      yandex_serverless_container.application.image[0].environment.CLOUD_OIDC_AUDIENCE == var.identity.client_id &&
      yandex_serverless_container.application.image[0].environment.CLOUD_OIDC_JWKS_URI == "https://auth.yandex.cloud/oauth/jwks/keys"
    )
    error_message = "The application must independently verify the same OIDC identity."
  }
  assert {
    condition = (
      !strcontains(lower(yandex_api_gateway.probe.spec), "basic") &&
      !strcontains(lower(yandex_api_gateway.probe.spec), "www-authenticate") &&
      !strcontains(yandex_api_gateway.probe.spec, "cloud_functions") &&
      !strcontains(yandex_api_gateway.probe.spec, "__Host-zont_oidc")
    )
    error_message = "Browser content must not invoke functions or accept legacy cookies/Basic."
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
  assert {
    condition     = length(yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes) == 0
    error_message = "The default isolated gateway must not create an OIDC security scheme."
  }

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
    condition     = alltrue([for path in ["/", "/index.html", "/latest.html", "/reports.json", "/daily/{file}", "/weekly/{file}", "/monthly/{file}", "/seasonal/{file}"] : yamldecode(yandex_api_gateway.probe.spec).paths[path]["get"]["x-yc-apigateway-integration"].container_id == "application-container"])
    error_message = "Published application content routes must invoke the protected application container."
  }
  assert {
    condition     = alltrue([for route in [{ path = "/daily/{file}", parameter = "file" }, { path = "/weekly/{file}", parameter = "file" }, { path = "/monthly/{file}", parameter = "file" }, { path = "/seasonal/{file}", parameter = "file" }, { path = "/api/{path+}", parameter = "path" }, { path = "/za/{path+}", parameter = "path" }, { path = "/za/api/{path+}", parameter = "path" }] : anytrue([for parameter in yamldecode(yandex_api_gateway.probe.spec).paths[route.path].parameters : parameter.name == route.parameter && parameter.in == "path" && parameter.required && parameter.schema.type == "string"])])
    error_message = "Every templated gateway route must declare its required string path parameter."
  }
  assert {
    condition     = alltrue([for method in ["get", "put", "post"] : yamldecode(yandex_api_gateway.probe.spec).paths["/api/{path+}"][method]["x-yc-apigateway-integration"].container_id == "application-container"])
    error_message = "The greedy API route must forward GET, PUT, and POST to the application container."
  }
  assert {
    condition     = yamldecode(yandex_api_gateway.probe.spec).paths["/za/"]["get"]["x-yc-apigateway-integration"].container_id == "application-container" && yamldecode(yandex_api_gateway.probe.spec).paths["/za/{path+}"]["get"]["x-yc-apigateway-integration"].container_id == "application-container"
    error_message = "Legacy /za page routes must invoke the protected application container."
  }
  assert {
    condition     = alltrue([for method in ["get", "put", "post"] : yamldecode(yandex_api_gateway.probe.spec).paths["/za/api/{path+}"][method]["x-yc-apigateway-integration"].container_id == "application-container"])
    error_message = "The legacy API route must forward GET, PUT, and POST to the application container."
  }
  assert {
    condition     = alltrue([for path in ["/jobs/publication", "/jobs/maintenance"] : yamldecode(yandex_api_gateway.probe.spec).paths[path]["post"]["x-yc-apigateway-integration"].container_id == "application-container"])
    error_message = "Publication and maintenance jobs must invoke the application container."
  }
  assert {
    condition     = toset(keys(yamldecode(yandex_api_gateway.probe.spec).paths["/login"])) == toset(["get", "post"]) && alltrue([for method in ["get", "post"] : yamldecode(yandex_api_gateway.probe.spec).paths["/login"][method]["x-yc-apigateway-integration"].container_id == "application-container"]) && toset(keys(yamldecode(yandex_api_gateway.probe.spec).paths["/logout"])) == toset(["post"]) && yamldecode(yandex_api_gateway.probe.spec).paths["/logout"]["post"]["x-yc-apigateway-integration"].container_id == "application-container" && !contains(keys(yamldecode(yandex_api_gateway.probe.spec).paths), "/internal/maintenance")
    error_message = "Login GET/POST and logout POST must target the application, while the private maintenance route stays off the gateway."
  }
  assert {
    condition     = yandex_storage_bucket_iam_binding.application_uploader.role == "storage.uploader" && yandex_storage_bucket_iam_binding.application_uploader.bucket == yandex_storage_bucket.publication.bucket && yandex_storage_bucket_iam_binding.probe.role == "storage.viewer"
    error_message = "The runtime needs upload access while the existing viewer binding remains in place."
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

run "maintenance_timer_requires_metrics" {
  command = plan
  variables {
    enable_maintenance_timer = true
  }
  expect_failures = [var.enable_maintenance_timer]
}

run "monitored_maintenance_timer" {
  command = plan
  variables {
    enable_maintenance_timer = true
    grafana_metrics_enabled  = true
  }
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
    condition     = length(yandex_function_trigger.maintenance) == 1 && yandex_function_trigger.maintenance[0].container[0].id == yandex_serverless_container.application.id && yandex_function_trigger.maintenance[0].container[0].path == "/internal/maintenance"
    error_message = "The opt-in maintenance timer must invoke the private application maintenance endpoint."
  }
  assert {
    condition     = !contains(keys(yamldecode(yandex_api_gateway.probe.spec).paths), "/internal/maintenance")
    error_message = "The private maintenance endpoint must not be exposed through the public gateway."
  }
}

run "monitoring_is_private_and_opt_in" {
  command = plan
  variables {
    enable_monitoring_timer = true
  }
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
    condition     = length(yandex_function_trigger.monitoring) == 1 && yandex_function_trigger.monitoring[0].container[0].path == "/internal/monitoring" && yandex_function_trigger.monitoring[0].timer[0].cron_expression == "0 * * * ? *"
    error_message = "Monitoring must be hourly and use only the private application endpoint."
  }
  assert {
    condition     = !contains(keys(yamldecode(yandex_api_gateway.probe.spec).paths), "/internal/monitoring")
    error_message = "The monitoring timer endpoint must not be exposed by the public gateway."
  }
}

run "attached_domain_sets_public_origin" {
  command = plan
  variables {
    attach_domain = true
  }
  assert {
    condition     = yandex_serverless_container.application.image[0].environment.CLOUD_PUBLIC_ORIGIN == "https://${var.test_domain}"
    error_message = "An attached custom domain must be the application's exact public origin."
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

run "scheduler_requires_metrics" {
  command = plan
  variables {
    enable_scheduler_timer            = true
    application_config_secret_enabled = true
  }
  expect_failures = [var.enable_scheduler_timer]
}

run "scheduler_requires_writes" {
  command = plan
  variables {
    enable_scheduler_timer            = true
    grafana_metrics_enabled           = true
    application_config_secret_enabled = true
    application_writes_enabled        = false
  }
  expect_failures = [var.enable_scheduler_timer]
}

run "scheduler_requires_configuration" {
  command = plan
  variables {
    enable_scheduler_timer  = true
    grafana_metrics_enabled = true
  }
  expect_failures = [var.enable_scheduler_timer]
}

run "configured_scheduler_is_private" {
  command = plan
  variables {
    enable_scheduler_timer            = true
    grafana_metrics_enabled           = true
    application_config_secret_enabled = true
  }
  override_resource {
    target          = yandex_serverless_container.application
    override_during = plan
    values          = { id = "application-container" }
  }
  override_resource {
    target          = yandex_serverless_container.probe
    override_during = plan
    values          = { id = "probe-container" }
  }
  assert {
    condition     = length(yandex_function_trigger.scheduler) == 1 && yandex_function_trigger.scheduler[0].container[0].id == "application-container" && yandex_function_trigger.scheduler[0].container[0].path == "/internal/scheduler"
    error_message = "The configured scheduler must invoke the private application scheduler route."
  }
  assert {
    condition     = !contains(keys(yamldecode(yandex_api_gateway.probe.spec).paths), "/internal/scheduler") && yamldecode(yandex_api_gateway.probe.spec).paths["/jobs/scheduler"].post["x-yc-apigateway-integration"].container_id == "application-container"
    error_message = "The private timer route must stay off the gateway and the operational scheduler route must target the application."
  }
  assert {
    condition     = length([for secret in yandex_serverless_container.application.secrets : secret if secret.key == "application_config_json" && secret.environment_variable == "ZONT_ANALYZER_CONFIG_JSON" && secret.version_id == var.secret_version_id]) == 1
    error_message = "The application configuration must come from the selected Lockbox secret version."
  }
}

run "maintenance_disables_writes" {
  command = plan
  variables {
    application_writes_enabled = false
  }
  assert {
    condition     = yandex_serverless_container.application.image[0].environment.CLOUD_WRITES_ENABLED == "false" && length(yandex_function_trigger.scheduler) == 0
    error_message = "Maintenance must disable runtime writes and leave the scheduler off."
  }
}

run "migration_capacity_remains_capped" {
  command = plan
  variables {
    ydb_request_units_per_second = 100
    ydb_storage_size_limit_gib   = 5
  }
  assert {
    condition     = one(yandex_ydb_database_serverless.probe.serverless_database).enable_throttling_rcu_limit && one(yandex_ydb_database_serverless.probe.serverless_database).throttling_rcu_limit == 100 && one(yandex_ydb_database_serverless.probe.serverless_database).storage_size_limit == 5 && one(yandex_ydb_database_serverless.probe.serverless_database).provisioned_rcu_limit == 0
    error_message = "Migration capacity must remain explicitly capped without provisioned idle capacity."
  }
}

run "uncapped_capacity_rejected" {
  command = plan
  variables {
    ydb_request_units_per_second = 0
    ydb_storage_size_limit_gib   = 0
  }
  expect_failures = [var.ydb_request_units_per_second, var.ydb_storage_size_limit_gib]
}

run "production_publication_is_isolated" {
  command = plan
  variables {
    application_publication_prefix = "production"
    attach_domain                  = true
    identity = {
      client_id = "test-oidc-client"
      issuer    = "https://auth.yandex.cloud"
      mode      = "spa"
    }
  }
  override_resource {
    target          = yandex_serverless_container.application
    override_during = plan
    values          = { id = "test-application" }
  }
  override_resource {
    target          = yandex_serverless_container.probe
    override_during = plan
    values          = { id = "test-probe" }
  }
  assert {
    condition     = yandex_serverless_container.application.image[0].environment.CLOUD_PUBLICATION_PREFIX == "production" && yamldecode(yandex_api_gateway.probe.spec).paths["/reports.json"].get["x-yc-apigateway-integration"].object == "production/site-index.json" && yamldecode(yandex_api_gateway.probe.spec).paths["/objects/publication/{path+}"].get["x-yc-apigateway-integration"].object == "production/publication/{path}"
    error_message = "Publisher and protected gateway must switch to the same isolated object prefix."
  }
}
