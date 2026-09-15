# This root survives every application environment. Credentials come from
# the process environment, never from Terraform-managed access keys.
resource "yandex_resourcemanager_folder" "project" {
  cloud_id = var.cloud_id
  name     = var.project_folder_name
  lifecycle {
    prevent_destroy = true
  }
}

resource "yandex_kms_symmetric_key" "state" {
  folder_id         = yandex_resourcemanager_folder.project.id
  name              = "terraform-state"
  default_algorithm = "AES_256"
  rotation_period   = "8760h"
  lifecycle {
    prevent_destroy = true
  }
}

resource "yandex_storage_bucket" "state" {
  folder_id     = yandex_resourcemanager_folder.project.id
  bucket        = var.state_bucket_name
  force_destroy = false
  max_size      = 1073741824
  anonymous_access_flags {
    read        = false
    list        = false
    config_read = false
  }
  versioning {
    enabled = true
  }
  server_side_encryption_configuration {
    rule {
      apply_server_side_encryption_by_default {
        kms_master_key_id = yandex_kms_symmetric_key.state.id
        sse_algorithm     = "aws:kms"
      }
    }
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "yandex_iam_service_account" "deploy" {
  folder_id = yandex_resourcemanager_folder.project.id
  name      = "infrastructure-deploy"
}

output "folder_id" {
  value     = yandex_resourcemanager_folder.project.id
  sensitive = true
}

output "state_bucket" {
  value     = yandex_storage_bucket.state.bucket
  sensitive = true
}

output "state_key_id" {
  value     = yandex_kms_symmetric_key.state.id
  sensitive = true
}

output "deployment_service_account_id" {
  value     = yandex_iam_service_account.deploy.id
  sensitive = true
}
