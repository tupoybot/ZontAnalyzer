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

variable "application_writes_enabled" {
  description = "Allow API mutations and background jobs; disable during cutover maintenance."
  type        = bool
  default     = true
}

variable "application_config_secret_enabled" {
  description = "Load the application configuration JSON from the selected Lockbox secret version."
  type        = bool
  default     = false
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

variable "application_publication_prefix" {
  description = "Isolate production publication from rehearsal objects during cutover."
  type        = string
  default     = "reports"
  validation {
    condition     = can(regex("^[a-z][a-z0-9_-]{0,63}$", var.application_publication_prefix))
    error_message = "Use a single safe publication prefix."
  }
}

variable "enable_production_database" {
  description = "Create a separate production database and select it for the application; retain development data during cutover."
  type        = bool
  default     = false
}

variable "retain_development_database" {
  description = "Keep the original development database after switching the application to production."
  type        = bool
  default     = true
  validation {
    condition     = var.retain_development_database || var.enable_production_database
    error_message = "The development database can be omitted only when the production database is enabled."
  }
}

variable "production_database_name" {
  description = "Name of the separate production database."
  type        = string
  default     = "zont-prod"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.production_database_name))
    error_message = "Use a safe database resource name."
  }
}

variable "production_ydb_request_units_per_second" {
  description = "Production on-demand throughput cap; this does not request a cloud quota increase."
  type        = number
  default     = 50
  validation {
    condition     = var.production_ydb_request_units_per_second >= 10 && var.production_ydb_request_units_per_second <= 1000 && floor(var.production_ydb_request_units_per_second) == var.production_ydb_request_units_per_second
    error_message = "Use an integer throughput cap between 10 and 1000 request units per second."
  }
}

variable "production_ydb_storage_size_limit_gib" {
  description = "Production storage cap including isolated recovery copies."
  type        = number
  default     = 2
  validation {
    condition     = var.production_ydb_storage_size_limit_gib >= 1 && var.production_ydb_storage_size_limit_gib <= 100 && floor(var.production_ydb_storage_size_limit_gib) == var.production_ydb_storage_size_limit_gib
    error_message = "Use an integer storage cap between 1 and 100 GiB."
  }
}

variable "ydb_request_units_per_second" {
  description = "Explicit serverless throughput cap for the accepted workload, including migration."
  type        = number
  default     = 10
  validation {
    condition     = var.ydb_request_units_per_second >= 10 && var.ydb_request_units_per_second <= 1000 && floor(var.ydb_request_units_per_second) == var.ydb_request_units_per_second
    error_message = "Use an integer throughput cap between 10 and 1000 request units per second."
  }
}

variable "ydb_storage_size_limit_gib" {
  description = "Explicit storage cap including migration metadata and recovery copies."
  type        = number
  default     = 1
  validation {
    condition     = var.ydb_storage_size_limit_gib >= 1 && var.ydb_storage_size_limit_gib <= 100 && floor(var.ydb_storage_size_limit_gib) == var.ydb_storage_size_limit_gib
    error_message = "Use an integer storage cap between 1 and 100 GiB."
  }
}

variable "enable_scheduler_timer" {
  description = "Run the production scheduler after the verified M8 cutover."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_scheduler_timer || (var.grafana_metrics_enabled && var.application_writes_enabled && var.application_config_secret_enabled)
    error_message = "Production scheduling requires monitoring, enabled writes and explicit application configuration."
  }
}

variable "enable_maintenance_timer" {
  description = "Deliver durable web requests and publication work; enable after the M5 smoke."
  type        = bool
  default     = false
  validation {
    condition     = !var.enable_maintenance_timer || var.grafana_metrics_enabled
    error_message = "Configure metric export before enabling web job delivery."
  }
}

variable "identity" {
  description = "Private Identity Hub SPA integration. Null is for isolated pre-OIDC probes only."
  type = object({
    client_id = string
    issuer    = string
    mode      = string
  })
  default = null
  validation {
    condition     = var.identity == null ? true : var.identity.mode == "spa" && var.identity.issuer == "https://auth.yandex.cloud" && can(regex("^[a-zA-Z0-9_-]+$", var.identity.client_id))
    error_message = "Use the public SPA client and official Identity Hub issuer."
  }
}
variable "enable_monitoring_timer" {
  description = "Enable read-only application monitoring without provider calls."
  type        = bool
  default     = false
}

variable "monitoring_trigger_import_id" {
  description = "Existing monitoring trigger adopted through the v2 API, prepared privately from state."
  type        = string
  default     = null
}

variable "monitoring_timer_schedule" {
  description = "Monitoring schedule; application releases retain the existing timer schedule."
  type        = string
  default     = "0 * * * ? *"
}
