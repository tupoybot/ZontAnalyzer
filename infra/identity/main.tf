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
resource "yandex_iam_oauth_client" "spa" {
  folder_id              = var.folder_id
  name                   = "zont-reports-browser-dev"
  profile_id             = "user-agent"
  pkce_required          = true
  authentication_methods = ["none"]
  redirect_uris          = ["${var.public_origin}/auth/callback"]
  scopes                 = ["openid", "profile"]
}
resource "yandex_organizationmanager_idp_application_oauth_application" "spa" {
  organization_id = var.organization_id
  name            = "zont-reports-browser-dev"
  client_grant = {
    client_id         = yandex_iam_oauth_client.spa.id
    authorized_scopes = ["openid", "profile"]
  }
  group_claims_settings = { group_distribution_type = "NONE" }
}
resource "yandex_organizationmanager_idp_application_oauth_application_assignment" "spa_reviewer" {
  application_id = yandex_organizationmanager_idp_application_oauth_application.spa.id
  subject_id     = yandex_organizationmanager_idp_user.reviewer.id
}
output "integration" {
  sensitive = true
  value = {
    client_id      = yandex_iam_oauth_client.spa.id
    application_id = yandex_organizationmanager_idp_application_oauth_application.spa.id
    pool_id        = yandex_organizationmanager_idp_userpool.reports.id
    test_username  = yandex_organizationmanager_idp_user.reviewer.username
    test_user_id   = yandex_organizationmanager_idp_user.reviewer.id
  }
}
