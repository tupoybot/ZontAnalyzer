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

variable "github_repository_owner_id" {
  description = "Immutable GitHub owner ID from the repository API."
  type        = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_owner_id))
    error_message = "A numeric GitHub owner ID is required."
  }
}

variable "github_repository_id" {
  description = "Immutable GitHub repository ID from the repository API."
  type        = string
  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "A numeric GitHub repository ID is required."
  }
}
