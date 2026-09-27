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
    condition     = var.identity == null && length(yandex_function.auth) == 0 && length(yandex_function_iam_binding.auth) == 0 && !contains(keys(yandex_serverless_container.application.image[0].environment), "CLOUD_OIDC_ISSUER")
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
    condition     = yandex_serverless_container.application.memory == 512 && yandex_serverless_container.application.cores == 1 && yandex_serverless_container.application.core_fraction == 100 && yandex_serverless_container.application.concurrency == 1 && yandex_serverless_container.application.execution_timeout == "210s"
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
    condition     = length([for trigger in yandex_function_trigger.timer : trigger if trigger.container[0].id == yandex_serverless_container.application.id]) == 0
    error_message = "M2 application must not have a scheduler."
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
      client_id             = "test-oidc-client"
      issuer                = "https://issuer.example/test-pool"
      auth_service_account  = "test-auth-account"
      client_secret_id      = "test-client-secret"
      client_secret_version = "test-client-version"
      transaction_secret_id = "test-transaction-secret"
      transaction_version   = "test-transaction-version"
      code_sha256           = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
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
  override_resource {
    target          = yandex_function.auth[0]
    override_during = plan
    values          = { id = "test-auth-function" }
  }

  assert {
    condition = (
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].type == "jwt" &&
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].identitySource.in == "cookie" &&
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].identitySource.name == "__Host-zont_oidc" &&
      jsonencode(yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].issuers) == jsonencode([var.identity.issuer]) &&
      jsonencode(yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].audiences) == jsonencode([var.identity.client_id]) &&
      yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].jwksUri == "https://auth.yandex.cloud/oauth/jwks/keys" &&
      toset(yamldecode(yandex_api_gateway.probe.spec).components.securitySchemes.identityHub["x-yc-apigateway-authorizer"].requiredClaims) == toset(["sub", "exp", "iat"])
    )
    error_message = "The gateway must verify the host-only OIDC cookie against exactly the configured issuer and audience."
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
    condition = alltrue([for path, method in { "/auth/login" = "get", "/auth/callback" = "get", "/auth/logout" = "post" } : (
      yamldecode(yandex_api_gateway.probe.spec).paths[path][method]["x-yc-apigateway-integration"].type == "cloud_functions" &&
      yamldecode(yandex_api_gateway.probe.spec).paths[path][method]["x-yc-apigateway-integration"].function_id == "test-auth-function" &&
      yamldecode(yandex_api_gateway.probe.spec).paths[path][method]["x-yc-apigateway-integration"].service_account_id == var.timer_service_account_id
    )])
    error_message = "Authentication routes must invoke the dedicated private function through the gateway account."
  }
  assert {
    condition = (
      yandex_function.auth[0].folder_id == var.folder_id &&
      yandex_function.auth[0].service_account_id == var.identity.auth_service_account &&
      yandex_function.auth[0].user_hash == var.identity.code_sha256 &&
      yandex_function_iam_binding.auth[0].function_id == "test-auth-function" &&
      yandex_function_iam_binding.auth[0].role == "functions.functionInvoker" &&
      toset(yandex_function_iam_binding.auth[0].members) == toset(["serviceAccount:${var.timer_service_account_id}"]) &&
      length(yandex_function.auth[0].secrets) == 2 &&
      toset([for secret in yandex_function.auth[0].secrets : secret.key]) == toset(["client_secret", "transaction_key"]) &&
      alltrue([for secret in yandex_function.auth[0].secrets : (
        secret.key == "client_secret" ? (secret.id == var.identity.client_secret_id && secret.version_id == var.identity.client_secret_version && secret.environment_variable == "OIDC_CLIENT_SECRET") :
        secret.key == "transaction_key" && secret.id == var.identity.transaction_secret_id && secret.version_id == var.identity.transaction_version && secret.environment_variable == "OIDC_TRANSACTION_KEY"
      )])
    )
    error_message = "The auth function must use its own account and pinned secrets, with gateway-only invocation."
  }
  assert {
    condition = (
      yandex_function.auth[0].environment.OIDC_PUBLIC_ORIGIN == "https://${var.test_domain}" &&
      yandex_function.auth[0].environment.OIDC_ISSUER == var.identity.issuer &&
      yandex_function.auth[0].environment.OIDC_CLIENT_ID == var.identity.client_id &&
      !contains(keys(yandex_function.auth[0].environment), "OIDC_CLIENT_SECRET") &&
      !contains(keys(yandex_function.auth[0].environment), "OIDC_TRANSACTION_KEY") &&
      yandex_serverless_container.application.image[0].environment.CLOUD_OIDC_ISSUER == var.identity.issuer &&
      yandex_serverless_container.application.image[0].environment.CLOUD_OIDC_AUDIENCE == var.identity.client_id &&
      yandex_serverless_container.application.image[0].environment.CLOUD_OIDC_JWKS_URI == "https://auth.yandex.cloud/oauth/jwks/keys"
    )
    error_message = "App and function must agree on OIDC identity without exposing auth secrets in ordinary environment variables."
  }
  assert {
    condition = (
      !strcontains(lower(yandex_api_gateway.probe.spec), "basic") &&
      !strcontains(lower(yandex_api_gateway.probe.spec), "www-authenticate") &&
      yamldecode(yandex_api_gateway.probe.spec).paths["/login"].get["x-yc-apigateway-integration"].http_code == 303 &&
      yamldecode(yandex_api_gateway.probe.spec).paths["/login"].get["x-yc-apigateway-integration"].http_headers.Location == "/auth/login"
    )
    error_message = "Browser content must use OIDC login redirects and must never emit a Basic challenge."
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
