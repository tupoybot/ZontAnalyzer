resource "yandex_monitoring_dashboard" "probe" {
  folder_id = data.yandex_resourcemanager_folder.project.id
  name      = "${local.name}-probe"
  title     = "M1 infrastructure probe"
  widgets {
    chart {
      title = "Successful identity, storage and egress checks"
      queries {
        target {
          query     = "{folderId=\"${var.folder_id}\",service=\"custom\",name=\"m1_probe_health\"}"
          text_mode = true
        }
      }
    }
    position {
      x = 0
      y = 0
      w = 24
      h = 8
    }
  }
}
