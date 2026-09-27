mock_provider "yandex" {}

override_resource {
  target = yandex_organizationmanager_idp_userpool.reports
  values = { id = "test-local-pool", domains = ["local.example.test"] }
}
variables {
  folder_id       = "test-folder"
  organization_id = "test-organization"
  pool_subdomain  = "test-local"
  public_origin   = "https://reports.example.test"
  test_password   = "Synthetic-password-123!"
}
run "local_directory_and_least_privilege" {
  command = apply
  assert {
    condition     = yandex_organizationmanager_idp_user.reviewer.userpool_id == "test-local-pool" && yandex_organizationmanager_idp_user.reviewer.username == "reviewer@local.example.test" && yandex_organizationmanager_idp_user.reviewer.is_active
    error_message = "The review account must be a local pool user."
  }
  assert {
    condition     = yandex_organizationmanager_idp_application_oauth_application_assignment.spa_reviewer.subject_id == yandex_organizationmanager_idp_user.reviewer.id && yandex_organizationmanager_idp_application_oauth_application.spa.group_claims_settings.group_distribution_type == "NONE"
    error_message = "Access must be assigned to the local reviewer without importing groups."
  }
  assert {
    condition     = yandex_iam_oauth_client.spa.profile_id == "user-agent" && yandex_iam_oauth_client.spa.pkce_required && yandex_iam_oauth_client.spa.authentication_methods == tolist(["none"]) && yandex_iam_oauth_client.spa.redirect_uris == toset(["https://reports.example.test/auth/callback"])
    error_message = "The public browser client requires PKCE and an exact callback URI."
  }
  assert {
    condition     = yandex_organizationmanager_idp_userpool.reports.bruteforce_protection_policy.attempts == 5 && yandex_organizationmanager_idp_userpool.reports.bruteforce_protection_policy.block == "900s"
    error_message = "Local passwords require bounded failed-login attempts."
  }
}
