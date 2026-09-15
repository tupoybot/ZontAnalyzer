resource "yandex_iam_workload_identity_oidc_federation" "github" {
  folder_id = yandex_resourcemanager_folder.project.id
  name      = "github-deployment"
  issuer    = "https://token.actions.githubusercontent.com"
  jwks_url  = "https://token.actions.githubusercontent.com/.well-known/jwks"
  audiences = ["https://iam.api.cloud.yandex.net/iam/v1/tokens"]
}

resource "yandex_iam_workload_identity_federated_credential" "github" {
  service_account_id  = yandex_iam_service_account.deploy.id
  federation_id       = yandex_iam_workload_identity_oidc_federation.github.id
  external_subject_id = "repo:${var.github_repository}:environment:${var.github_environment}"
}

resource "yandex_kms_symmetric_key_iam_binding" "state" {
  symmetric_key_id = yandex_kms_symmetric_key.state.id
  role             = "kms.keys.encrypterDecrypter"
  members          = ["serviceAccount:${yandex_iam_service_account.deploy.id}"]
}

# Folder-scoped service administration. No cloud/billing/organization role.
# The GitHub environment must restrict which branches can use this identity.
resource "yandex_resourcemanager_folder_iam_member" "deployment" {
  for_each = toset([
    "container-registry.admin",
    "serverless.containers.admin",
    "functions.admin",
    "lockbox.admin",
    "ydb.admin",
    "storage.admin",
    "logging.admin",
    "monitoring.editor",
    "api-gateway.admin",
    "certificate-manager.admin",
    "resource-manager.viewer",
  ])
  folder_id = yandex_resourcemanager_folder.project.id
  role      = each.value
  member    = "serviceAccount:${yandex_iam_service_account.deploy.id}"
}

resource "yandex_iam_service_account" "runtime" {
  for_each  = toset(["dev", "pilot"])
  folder_id = yandex_resourcemanager_folder.project.id
  name      = "zont-${each.key}-runtime"
}

resource "yandex_iam_service_account" "timer" {
  for_each  = toset(["dev", "pilot"])
  folder_id = yandex_resourcemanager_folder.project.id
  name      = "zont-${each.key}-timer"
}

resource "yandex_iam_service_account_iam_binding" "deployment_runtime" {
  for_each           = yandex_iam_service_account.runtime
  service_account_id = each.value.id
  role               = "iam.serviceAccounts.user"
  members            = ["serviceAccount:${yandex_iam_service_account.deploy.id}"]
}

resource "yandex_iam_service_account_iam_binding" "deployment_timer" {
  for_each           = yandex_iam_service_account.timer
  service_account_id = each.value.id
  role               = "iam.serviceAccounts.user"
  members            = ["serviceAccount:${yandex_iam_service_account.deploy.id}"]
}

resource "yandex_resourcemanager_folder_iam_member" "runtime_logs" {
  for_each  = yandex_iam_service_account.runtime
  folder_id = yandex_resourcemanager_folder.project.id
  role      = "logging.writer"
  member    = "serviceAccount:${each.value.id}"
}

resource "yandex_resourcemanager_folder_iam_member" "runtime_metrics" {
  for_each  = yandex_iam_service_account.runtime
  folder_id = yandex_resourcemanager_folder.project.id
  role      = "monitoring.editor"
  member    = "serviceAccount:${each.value.id}"
}

output "runtime_service_accounts" {
  value     = { for environment, account in yandex_iam_service_account.runtime : environment => account.id }
  sensitive = true
}

output "timer_service_accounts" {
  value     = { for environment, account in yandex_iam_service_account.timer : environment => account.id }
  sensitive = true
}
