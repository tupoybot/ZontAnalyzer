terraform {
  required_version = "= 1.13.1"
  required_providers {
    yandex = {
      source  = "yandex-cloud/yandex"
      version = "= 0.228.0"
    }
  }
}

provider "yandex" {
  cloud_id = var.cloud_id
}
