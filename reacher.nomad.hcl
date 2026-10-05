# Reacher (check-if-email-exists) beside Bellwether, as its own Nomad job.
#
# Optional. Bellwether verifies email addresses with its own built-in SMTP
# check and needs nothing else, as long as this server can open outbound
# connections on port 25. Run Reacher as well when you want its provider
# specific handling, or to send checks through a SOCKS proxy with a clean
# mail reputation (set RCH__PROXY__HOST and friends below).
#
# It is a separate job on purpose: a problem pulling or starting this image can
# never hold up a Bellwether deploy.
#
#   nomad job run reacher.nomad.hcl
#
# Then in Bellwether: Settings, Verification, Reacher server =
#   http://<this client's address>:8080
# and leave the engine on "auto". Keep port 8080 firewalled to the cluster: the
# backend answers anyone who can reach it.
#
# Licence: the Reacher backend is AGPL-3.0 (with a commercial licence on
# offer). Running the unmodified image for internal use is generally fine;
# confirm with whoever handles licensing before relying on it.

job "reacher" {
  datacenters = ["dc1"]
  type        = "service"

  group "reacher" {
    count = 1

    network {
      port "http" {
        static = 8080
        to     = 8080
      }
    }

    task "backend" {
      driver = "docker"

      config {
        # Pinned: a moving tag would change verification behaviour silently.
        image = "reacherhq/backend:v0.11.7"
        ports = ["http"]
      }

      env {
        RCH__HTTP_HOST = "0.0.0.0"
      }

      resources {
        cpu    = 300
        memory = 256
      }

      service {
        name     = "reacher"
        port     = "http"
        provider = "nomad"
      }
    }
  }
}
