mock_provider "yandex" {}

override_resource {
  target = yandex_organizationmanager_idp_userpool.reports
  values = { id = "test-local-pool", domains = ["local.example.test"] }
}
variables {
  folder_id                     = "test-folder"
  organization_id               = "test-organization"
  deployment_service_account_id = "test-deployer"
  pool_subdomain                = "test-local"
  public_origin                 = "https://reports.example.test"
  test_password                 = "Synthetic-password-123!"
  transaction_key               = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
}
run "local_directory_and_least_privilege" {
  command = apply
  assert {
    condition     = yandex_organizationmanager_idp_user.reviewer.userpool_id == "test-local-pool" && yandex_organizationmanager_idp_user.reviewer.username == "reviewer@local.example.test" && yandex_organizationmanager_idp_user.reviewer.is_active
    error_message = "The review account must be a local pool user."
  }
  assert {
    condition     = yandex_organizationmanager_idp_application_oauth_application_assignment.reviewer.subject_id == yandex_organizationmanager_idp_user.reviewer.id && yandex_organizationmanager_idp_application_oauth_application.reports.group_claims_settings.group_distribution_type == "NONE"
    error_message = "Access must be assigned to the local reviewer without importing groups."
  }
  assert {
    condition     = yandex_iam_oauth_client.reports.profile_id == "web" && yandex_iam_oauth_client.reports.pkce_required && yandex_iam_oauth_client.reports.authentication_methods == tolist(["client_secret_basic"]) && yandex_iam_oauth_client.reports.redirect_uris == toset(["https://reports.example.test/auth/callback"])
    error_message = "The confidential client requires PKCE and an exact callback URI."
  }
  assert {
    condition     = yandex_organizationmanager_idp_userpool.reports.bruteforce_protection_policy.attempts == 5 && yandex_organizationmanager_idp_userpool.reports.bruteforce_protection_policy.block == "900s"
    error_message = "Local passwords require bounded failed-login attempts."
  }
  assert {
    condition     = yandex_lockbox_secret_iam_binding.client.members == toset(["serviceAccount:${yandex_iam_service_account.auth.id}"]) && yandex_lockbox_secret_iam_binding.transaction.members == toset(["serviceAccount:${yandex_iam_service_account.auth.id}"]) && yandex_iam_service_account_iam_binding.deployer.role == "iam.serviceAccounts.user"
    error_message = "Only the auth runtime can read its secrets; deployment gets a scoped service-account assignment."
  }
}
