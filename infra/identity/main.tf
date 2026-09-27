terraform {
  required_version = "= 1.13.1"
  required_providers {
    yandex = { source = "yandex-cloud/yandex", version = "= 0.228.0" }
  }
}
provider "yandex" { folder_id = var.folder_id }
variable "folder_id" { type = string }
variable "organization_id" { type = string }
variable "pool_subdomain" { type = string }
variable "public_origin" { type = string }
variable "test_password" {
  type      = string
  sensitive = true
}
variable "transaction_key" {
  type      = string
  sensitive = true
}
resource "yandex_organizationmanager_idp_userpool" "reports" {
  organization_id   = var.organization_id
  name              = "zont-reports-dev"
  default_subdomain = var.pool_subdomain
  user_settings = {
    allow_edit_self_login    = false
    allow_edit_self_password = true
  }
  bruteforce_protection_policy = {
    attempts = 5
    window   = "900s"
    block    = "900s"
  }
}
resource "yandex_organizationmanager_idp_user" "reviewer" {
  userpool_id   = yandex_organizationmanager_idp_userpool.reports.id
  username      = "reviewer@${yandex_organizationmanager_idp_userpool.reports.domains[0]}"
  full_name     = "Report reviewer"
  is_active     = true
  password_spec = { password = var.test_password }
  lifecycle { ignore_changes = [password_spec] }
}
resource "yandex_iam_oauth_client" "reports" {
  folder_id              = var.folder_id
  name                   = "zont-reports-dev"
  profile_id             = "web"
  pkce_required          = true
  authentication_methods = ["client_secret_basic"]
  redirect_uris          = ["${var.public_origin}/auth/callback"]
  scopes                 = ["openid", "profile"]
}
resource "yandex_organizationmanager_idp_application_oauth_application" "reports" {
  organization_id = var.organization_id
  name            = "zont-reports-dev"
  client_grant = {
    client_id         = yandex_iam_oauth_client.reports.id
    authorized_scopes = ["openid", "profile"]
  }
  group_claims_settings = { group_distribution_type = "NONE" }
}
resource "yandex_organizationmanager_idp_application_oauth_application_assignment" "reviewer" {
  application_id = yandex_organizationmanager_idp_application_oauth_application.reports.id
  subject_id     = yandex_organizationmanager_idp_user.reviewer.id
}
resource "yandex_iam_service_account" "auth" {
  folder_id = var.folder_id
  name      = "zont-reports-auth-dev"
}
resource "yandex_lockbox_secret" "client" {
  folder_id = var.folder_id
  name      = "zont-reports-oidc-dev"
}
resource "yandex_iam_oauth_client_secret" "reports" {
  oauth_client_id = yandex_iam_oauth_client.reports.id
  output_to_lockbox {
    secret_id              = yandex_lockbox_secret.client.id
    entry_for_secret_value = "client_secret"
  }
}
resource "yandex_lockbox_secret" "transaction" {
  folder_id = var.folder_id
  name      = "zont-reports-transaction-dev"
}
resource "yandex_lockbox_secret_version" "transaction" {
  secret_id = yandex_lockbox_secret.transaction.id
  entries {
    key        = "transaction_key"
    text_value = var.transaction_key
  }
}
resource "yandex_lockbox_secret_iam_binding" "client" {
  secret_id = yandex_lockbox_secret.client.id
  role      = "lockbox.payloadViewer"
  members   = ["serviceAccount:${yandex_iam_service_account.auth.id}"]
}
resource "yandex_lockbox_secret_iam_binding" "transaction" {
  secret_id = yandex_lockbox_secret.transaction.id
  role      = "lockbox.payloadViewer"
  members   = ["serviceAccount:${yandex_iam_service_account.auth.id}"]
}
output "integration" {
  sensitive = true
  value = {
    client_id             = yandex_iam_oauth_client.reports.id
    application_id        = yandex_organizationmanager_idp_application_oauth_application.reports.id
    pool_id               = yandex_organizationmanager_idp_userpool.reports.id
    test_username         = yandex_organizationmanager_idp_user.reviewer.username
    test_user_id          = yandex_organizationmanager_idp_user.reviewer.id
    auth_service_account  = yandex_iam_service_account.auth.id
    client_secret_id      = yandex_lockbox_secret.client.id
    client_secret_version = yandex_iam_oauth_client_secret.reports.output_to_lockbox_version_id
    transaction_secret_id = yandex_lockbox_secret.transaction.id
    transaction_version   = yandex_lockbox_secret_version.transaction.id
  }
}

variable "deployment_service_account_id" { type = string }
resource "yandex_iam_service_account_iam_binding" "deployer" {
  service_account_id = yandex_iam_service_account.auth.id
  role               = "iam.serviceAccounts.user"
  members            = ["serviceAccount:${var.deployment_service_account_id}"]
}
