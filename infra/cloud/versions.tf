terraform {
  required_version = "= 1.13.1"
  backend "s3" {}
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "= 0.228.0"
    }
  }
}

provider "yandex" {
  cloud_id  = var.cloud_id
  folder_id = var.folder_id
}

data "yandex_resourcemanager_folder" "project" {
  folder_id = var.folder_id
  lifecycle {
    postcondition {
      condition     = self.cloud_id == var.cloud_id
      error_message = "The selected folder is outside the authorized cloud."
    }
  }
}
