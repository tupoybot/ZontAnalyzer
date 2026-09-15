variable "cloud_id" {
  type      = string
  sensitive = true
}

variable "project_folder_name" {
  type      = string
  sensitive = true
}

variable "state_bucket_name" {
  type      = string
  sensitive = true
}

variable "github_repository" {
  description = "Public owner/repository that may deploy through the protected environment."
  type        = string
}

variable "github_environment" {
  type = string
}
