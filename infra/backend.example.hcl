# Copy outside Git, replace placeholders, and chmod 600 before init.
bucket = "REPLACE_WITH_PRIVATE_STATE_BUCKET"
key    = "dev/terraform.tfstate"
region = "ru-central1"
endpoints = {
  s3 = "https://storage.yandexcloud.net"
}
use_lockfile                = true
# SSE-KMS is enforced by the bucket default, verified with HeadObject.
# Terraform's kms_key_id validator accepts AWS key formats, not Yandex IDs.
skip_credentials_validation = true
skip_region_validation      = true
skip_requesting_account_id  = true
skip_metadata_api_check     = true
use_path_style              = true
skip_s3_checksum            = true
