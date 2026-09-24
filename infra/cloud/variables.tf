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
  description = "Accepted M2 application artifact in Yandex Container Registry, selected by immutable digest."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^cr\\.yandex/[^@]+@sha256:[0-9a-f]{64}$", var.application_image))
    error_message = "Select an immutable Yandex Registry application image by digest."
  }
}

variable "application_revision" {
  description = "Full Git revision that produced the selected application image."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.application_revision))
    error_message = "Provide the full 40-character lowercase Git revision."
  }
}

variable "application_ydb_namespace" {
  description = "Explicit isolated application schema selected for import and runtime."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,63}$", var.application_ydb_namespace))
    error_message = "Use a YDB namespace of up to 64 letters, digits and underscores, starting with a letter."
  }
}

variable "openai_smoke_model" {
  description = "Exact selected model used only by the bounded M2 OpenAI access check."
  type        = string
  validation {
    condition     = length(trimspace(var.openai_smoke_model)) > 0
    error_message = "Select the exact model for the bounded OpenAI access check."
  }
}

variable "openai_access_confirmed" {
  description = "Set privately only after the owner confirms the M0 OpenAI access decision."
  type        = bool
  default     = false
}

variable "enable_timer" {
  description = "Enable only after manual runtime smoke succeeds."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_timer || var.grafana_metrics_enabled
    error_message = "Configure Grafana metric export before enabling scheduled checks."
  }
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

variable "grafana_metrics_enabled" {
  description = "Enable after adding the scoped OTLP credentials to the selected Lockbox version."
  type        = bool
  default     = false
}

variable "deletion_protection" {
  description = "Protect the database and certificate; disable explicitly before tearing down an isolated test stack."
  type        = bool
  default     = true
}
