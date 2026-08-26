# Bellwether on Nomad.
#
# The same two containers as docker-compose.yml -- the app, and Caddy in front
# of it terminating TLS -- expressed as one Nomad job. What changes under an
# orchestrator, and why this is not a mechanical translation of the compose file:
#
#   Nomad does not build images. Compose had `build: .`; here the image must
#   already exist in a registry the client can pull from. Build and push before
#   every deploy, or Nomad will place the previous image and the deploy will
#   look like it worked.
#
#   count = 1 and canary = 0 are load-bearing, not defaults left alone. This app
#   is a single 2.2GB SQLite file with background jobs that write to it for
#   days. A second allocation -- a scaled replica, or a canary running beside
#   the old one during a deploy -- means two writers on one file, which is how
#   the database gets corrupted rather than merely slow.
#
#   The data lives on a host volume, so the job is pinned to whichever client
#   declares that volume. That is deliberate. Nothing here is designed to
#   relocate; DEPLOY.md Option C explains the same tradeoff for Fly and Render.
#
# ---------------------------------------------------------------------------
# Prerequisites on the Nomad client that will run this
#
#   1. CNI plugins installed. The group uses bridge networking so Caddy can
#      reach the app on localhost while the app is never bound to the host:
#        https://developer.hashicorp.com/nomad/docs/networking/cni
#
#   2. Two host volumes declared in the client config, then a client restart:
#
#        # /etc/nomad.d/client.hcl
#        client {
#          host_volume "bellwether_data"  { path = "/opt/bellwether/data" }
#          host_volume "bellwether_caddy" { path = "/opt/bellwether/caddy" }
#        }
#
#      The app image runs as uid 10001 and the Caddy image as uid 1000, so each
#      directory has to be writable by the uid that will use it:
#
#        mkdir -p /opt/bellwether/data /opt/bellwether/caddy
#        chown -R 10001:10001 /opt/bellwether/data
#        chown -R 1000:1000   /opt/bellwether/caddy
#
#      Caddy's volume holds the ACME account key and the issued certificates.
#      Losing it means re-issuing on every restart, and Let's Encrypt rate
#      limits that.
#
#   3. The two secrets, as Nomad variables rather than as anything written here:
#
#        nomad var put nomad/jobs/bellwether \
#          secret=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
#          contact=contact@acumen-strategy.com
#
# Deploy
#
#   docker build -t registry.example.com/bellwether:2026-08-27 .
#   docker push  registry.example.com/bellwether:2026-08-27
#
#   nomad job run \
#     -var image=registry.example.com/bellwether:2026-08-27 \
#     -var hostname=bellwether.acumen-strategy.com \
#     bellwether.nomad.hcl
#
# Then create the accounts. They live in the volume and survive redeploys:
#
#   nomad alloc exec -task app -job bellwether \
#     python -m scripts.manage_users add rahul --name "Rahul Gopan"
#
# Getting the existing database there is a file copy onto the host volume, done
# with the job stopped so nothing is mid-write:
#
#   nomad job stop bellwether
#   scp prospect.db root@<client>:/opt/bellwether/data/
#   scp -r data/snapshots root@<client>:/opt/bellwether/data/
#   ssh root@<client> chown -R 10001:10001 /opt/bellwether/data
#   nomad job start bellwether
#
# Backups are unchanged in spirit -- .backup, never a plain copy of a live file:
#
#   nomad alloc exec -task app -job bellwether \
#     sqlite3 /data/prospect.db ".backup /data/backup.db"
# ---------------------------------------------------------------------------

variable "image" {
  type        = string
  description = <<-EOT
    Fully qualified image reference, including a real tag. Never :latest --
    Nomad cannot tell you which build is running if every build has the same
    name, and auto_revert has nothing to revert to.
  EOT
}

variable "hostname" {
  type        = string
  description = "Public hostname with an A record pointing at this client. Caddy uses it to obtain the certificate."
}

variable "datacenters" {
  type    = list(string)
  default = ["dc1"]
}

job "bellwether" {
  datacenters = var.datacenters
  type        = "service"

  # A destructive update stops the old allocation before starting the new one,
  # which is the only safe order when both would open the same SQLite file.
  # canary = 0 is the whole point: a canary runs *alongside* the old allocation.
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
    # Not a tuning knob. See the header.
    count = 1

    # Bridge gives the two tasks one network namespace, so Caddy reaches the app
    # on 127.0.0.1 and the app is never published to the host -- the same
    # property the compose file got from `expose` rather than `ports`.
    network {
      mode = "bridge"

      port "http" {
        static = 80
        to     = 80
      }

      port "https" {
        static = 443
        to     = 443
      }

      # Mapped to a random host port only so Nomad's health check has an address
      # to talk to. Firewall the client to 80, 443 and SSH: /healthz is the one
      # route on this port that needs no session, and everything else on it
      # should be reached through Caddy over TLS.
      port "app" {
        to = 8787
      }
    }

    volume "data" {
      type      = "host"
      source    = "bellwether_data"
      read_only = false
    }

    volume "caddy" {
      type      = "host"
      source    = "bellwether_caddy"
      read_only = false
    }

    # A failing app here is usually a bad image or a full disk, and restarting
    # into the same wall every few seconds helps nobody. Fail the group and let
    # reschedule back off instead.
    restart {
      attempts = 2
      interval = "10m"
      delay    = "30s"
      mode     = "fail"
    }

    reschedule {
      delay          = "30s"
      delay_function = "exponential"
      max_delay      = "10m"
      unlimited      = true
    }

    task "app" {
      driver = "docker"

      config {
        image = var.image
        ports = ["app"]
      }

      volume_mount {
        volume      = "data"
        destination = "/data"
        read_only   = false
      }

      # All five are already set in the image. They are repeated here because
      # this file is where someone will look to find out where the state lives,
      # and a jobspec that silently depends on the image's ENV is a jobspec that
      # breaks quietly when the image changes.
      env {
        BELLWETHER_DB    = "/data/prospect.db"
        BELLWETHER_DATA  = "/data"
        BELLWETHER_HTTPS = "1"

        # Accounts, on the volume rather than in the image. In config/ they
        # would be created successfully, work until the next deploy, and then
        # vanish with the allocation that held them.
        BELLWETHER_USERS = "/data/users.yml"

        # Tells the app it does not own its own lifetime: hides the sidebar's
        # Quit link, turns /quit into an explanation instead of a shutdown, and
        # clears the pidfiles left in the volume by whichever allocation died.
        # Without it, one click in the sidebar stops the server under everyone
        # and Nomad restarts it a moment later, which reads as a crash.
        BELLWETHER_MANAGED = "1"
      }

      # Secrets come from Nomad variables, never from this file -- it is tracked,
      # and a key written here would stay in the history after it was rotated.
      # Changing the signing key signs everyone out, which is the correct
      # behaviour, so restarting on change is the honest response to it.
      template {
        data        = <<-EOT
          {{ with nomadVar "nomad/jobs/bellwether" }}
          BELLWETHER_SECRET={{ .secret }}
          BELLWETHER_CONTACT={{ .contact }}
          {{ end }}
        EOT
        destination = "secrets/app.env"
        env         = true
        change_mode = "restart"
      }

      # Measured, not guessed, in DEPLOY.md: the supervisor plus two workers sit
      # near 105MB together, and the heaviest background job -- brochure parsing
      # under pdfplumber -- peaks near 250MB. memory_max leaves room for that
      # peak without reserving it against every other job on the client.
      resources {
        cpu        = 1000
        memory     = 512
        memory_max = 1024
      }

      # SIGTERM, then time to finish the transaction in flight. Killing a writer
      # mid-transaction is survivable under WAL, but there is no reason to make
      # SQLite prove that on every deploy.
      kill_timeout = "30s"

      service {
        name     = "bellwether"
        port     = "app"
        provider = "nomad"

        check {
          type     = "http"
          path     = "/healthz"
          interval = "30s"
          timeout  = "5s"

          # The grace covers opening a 2.2GB database on a cold page cache.
          check_restart {
            limit           = 3
            grace           = "45s"
            ignore_warnings = false
          }
        }
      }
    }

    task "caddy" {
      driver = "docker"

      config {
        image = "caddy:2-alpine"
        ports = ["http", "https"]
        args  = ["caddy", "run", "--config", "/local/Caddyfile", "--adapter", "caddyfile"]
      }

      volume_mount {
        volume      = "caddy"
        destination = "/data"
        read_only   = false
      }

      # Kept deliberately identical to ./Caddyfile, with the upstream changed
      # from the compose service name to localhost because the two tasks share
      # one network namespace here. If you change one, change the other.
      template {
        data          = <<-EOT
          ${var.hostname} {
          	encode gzip

          	# Brochure PDFs and the CSV export can be large and slow to assemble,
          	# and the weekly cycle button returns only after handing off, so give
          	# the app room rather than cutting a response in half.
          	reverse_proxy 127.0.0.1:8787 {
          		header_up X-Forwarded-Proto https
          		transport http {
          			read_timeout 300s
          		}
          	}

          	# The app sets HttpOnly, Secure, SameSite=Strict on the session
          	# cookie; HSTS is what stops a first request ever going out over
          	# plain http.
          	header {
          		Strict-Transport-Security "max-age=31536000; includeSubDomains"
          		X-Content-Type-Options "nosniff"
          		X-Frame-Options "DENY"
          		Referrer-Policy "same-origin"
          		-Server
          	}

          	log {
          		output file /data/access.log {
          			roll_size 10MiB
          			roll_keep 5
          		}
          	}
          }
        EOT
        destination   = "local/Caddyfile"
        change_mode   = "signal"
        change_signal = "SIGUSR1"
      }

      resources {
        cpu    = 200
        memory = 128
      }
    }
  }
}
