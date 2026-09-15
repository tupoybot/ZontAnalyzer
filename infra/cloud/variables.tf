variable "cloud_id" {
  description = "Authorized cloud from protected environment configuration."
  type        = string
  sensitive   = true
}

variable "folder_id" {
  type      = string
  sensitive = true
}

variable "runtime_service_account_id" {
  type      = string
  sensitive = true
}

variable "timer_service_account_id" {
  type      = string
  sensitive = true
}

variable "smoke_url" {
  type      = string
  sensitive = true
}

variable "test_domain" {
  type      = string
  sensitive = true
}

variable "attach_domain" {
  description = "Set only after the managed certificate is issued."
  type        = bool
  default     = false
}

variable "environment" {
  type = string
  validation {
    condition     = contains(["dev", "pilot"], var.environment)
    error_message = "M1 permits isolated dev or pilot environments only."
  }
}

variable "publication_bucket_name" {
  type      = string
  sensitive = true
}

variable "probe_image" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^cr\\.yandex/[^@]+@sha256:[0-9a-f]{64}$", var.probe_image))
    error_message = "Select an immutable Yandex Registry probe image by digest."
  }
}

variable "application_image" {
  description = "Accepted main artifact for subsequent compatibility work; not executed by the M1 probe."
  type        = string
  validation {
    condition     = can(regex("^[^@]+@sha256:[0-9a-f]{64}$", var.application_image))
    error_message = "Select an accepted application image by digest."
  }
}

variable "enable_timer" {
  description = "Enable only after manual runtime smoke succeeds."
  type        = bool
  default     = false
}

variable "timer_schedule" {
  description = "Hourly by default; minute cadence is only for bounded acceptance."
  type        = string
  default     = "0 * * * ? *"
  validation {
    condition     = contains(["0 * * * ? *", "* * * * ? *"], var.timer_schedule)
    error_message = "Use the hourly schedule or the temporary acceptance schedule."
  }
}

variable "secret_version_id" {
  description = "Version uploaded privately after creation of the empty Lockbox secret."
  type        = string
  sensitive   = true
}
