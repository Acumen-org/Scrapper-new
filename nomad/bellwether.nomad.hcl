variable "image_tag" {
  type    = string
  default = "2026-09-02-nulfix"
}

variable "harbor_username" {
  type    = string
  default = "admin"
}

variable "harbor_password" {
  type = string
}

variable "reacher_secret" {
  type = string
}

locals {
  registry = "h4rb0r.pmx.acumen-strategy.com"
  image    = "${local.registry}/bellwether/app:${var.image_tag}"
}

job "bellwether" {
  datacenters = ["acumen-dc"]
  type        = "service"
  namespace   = "default"

  constraint {
    attribute = "${attr.unique.hostname}"
    value     = "worker-5"
  }

  # Destructive update: stop the old alloc before starting the new one.
  # Two processes must never open the same SQLite file.
  update {
    max_parallel      = 1
    canary            = 0
    health_check      = "checks"
    min_healthy_time  = "20s"
    healthy_deadline  = "3m"
    progress_deadline = "10m"
    auto_revert       = true
  }

  group "web" {
    # Load-bearing. One SQLite writer, never two.
    count = 1

    network {
      port "reacher" {
        to = 8080
        host_network = "default"
      }
      port "app" {
        static       = 8787
        to           = 8787
        host_network = "default"
      }
    }

    restart {
      attempts = 3
      interval = "10m"
      delay    = "30s"
      mode     = "delay"
    }

    reschedule {
      delay          = "30s"
      delay_function = "exponential"
      max_delay      = "10m"
      unlimited      = true
    }

    task "app" {
      driver = "docker"

      consul {}

      config {
        image       = local.image
        ports       = ["app"]
        args = [
          "python", "-m", "uvicorn", "prospect.webapp:app",
          "--host", "0.0.0.0", "--port", "8787",
          "--workers", "1", "--log-level", "warning",
        ]
        dns_servers = ["172.17.0.1", "8.8.8.8"]
        volumes     = ["/opt/bellwether/data:/data"]

        auth {
          server_address = local.registry
          username       = var.harbor_username
          password       = var.harbor_password
        }
      }

      env {
        BELLWETHER_DATA    = "/data"
        BELLWETHER_HTTPS   = "1"
        BELLWETHER_USERS   = "/data/users.yml"
        BELLWETHER_MANAGED = "1"
        BELLWETHER_REACHER_URL = "http://${NOMAD_ADDR_reacher}"
        BELLWETHER_REACHER_SECRET = var.reacher_secret
      }

      template {
        data        = <<-EOT
          {{ with nomadVar "nomad/jobs/bellwether" }}
          BELLWETHER_SECRET={{ .secret }}
          BELLWETHER_CONTACT={{ .contact }}
          BELLWETHER_DSN={{ .dsn }}
          {{ end }}
        EOT
        destination = "secrets/app.env"
        env         = true
        change_mode = "restart"
      }

      # memory_max rose from 1024: website reading renders the occasional
      # JavaScript-only page in a headless browser (one at a time, about 250MB
      # while it runs) beside the app and its background jobs. The reservation
      # is unchanged, so placement on worker-5 is unaffected.
      # cpu went from 1000 to 800 when the reacher task joined the group with
      # 200: worker-5 has no CPU headroom (placement failed with cpu exhausted),
      # so the group keeps the 1000 it has always reserved. Docker CPU shares
      # are relative, not a ceiling: the app still uses idle CPU freely.
      resources {
        cpu        = 800
        memory     = 512
        memory_max = 1536
      }

      kill_timeout = "30s"

      service {
        name = "bellwether"
        port = "app"
        tags = ["bellwether", "prospect"]

        check {
          type     = "http"
          path     = "/healthz"
          interval = "30s"
          timeout  = "5s"
          check_restart {
            limit           = 3
            grace           = "45s"
            ignore_warnings = false
          }
        }
      }
    }

    # Authenticated internal service; it has no public ingress route.
    task "reacher" {
      driver = "docker"
      consul {}
      config {
        image = "reacherhq/backend:v0.11.7"
        ports = ["reacher"]
        dns_servers = ["172.17.0.1", "8.8.8.8"]
      }
      env {
        RCH__HEADER_SECRET = var.reacher_secret
        RCH__HELLO_NAME = "acumen-strategy.com"
        RCH__FROM_EMAIL = "verify@acumen-strategy.com"
        RCH__SMTP_TIMEOUT = "45"
        RCH__WORKER__ENABLE = "false"
      }
      resources {
        cpu = 200
        memory = 256
        memory_max = 768
      }
      service {
        name = "bellwether-reacher"
        port = "reacher"
        check {
          type = "tcp"
          interval = "20s"
          timeout = "5s"
        }
      }
    }
  }
}
