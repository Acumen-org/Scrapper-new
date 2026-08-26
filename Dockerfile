# Bellwether, containerised.
#
# The image holds only code. The database, the captured SEC snapshots, the
# brochure cache and the session key all live on a mounted volume at /data,
# because they are gigabytes, they must survive a redeploy, and they are the one
# thing here that cannot be rebuilt from this repository in a hurry.

FROM python:3.11-slim

# tini reaps zombies: the app spawns autopilot and weekly-cycle subprocesses,
# and PID 1 in a container does not reap children by default.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini curl sqlite3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code edits do not invalidate the wheel layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prospect/ ./prospect/
COPY scripts/ ./scripts/
COPY config/ ./config/

# Runs unprivileged.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bellwether

# /data has to exist, and be owned by the app, before VOLUME declares it. Docker
# seeds a fresh named volume from whatever is at the mount point in the image,
# ownership included; with no /data here it invents one owned by root and the
# app cannot write its own database, accounts, or session key into it. A bind
# mount takes its owner from the host instead, which is why the Nomad job tells
# you to chown the host volume by hand.
RUN mkdir -p /data && chown 10001:10001 /data

# config/ is the one code directory written at runtime: the weekly cycle
# regenerates target_securities.yml through scripts.build_cusip_map. COPY leaves
# it owned by root, and the app is not root, so without this the weekly cycle
# dies on a permission error the first time a CUSIP refresh comes due.
RUN chown -R 10001:10001 /app/config

USER 10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BELLWETHER_DATA=/data \
    BELLWETHER_DB=/data/prospect.db \
    BELLWETHER_HTTPS=1

# Accounts belong on the volume, not in the image. Left in config/ they are
# created successfully, work until the next deploy, and then vanish with the
# container that held them, locking everyone out of a running server.
ENV BELLWETHER_USERS=/data/users.yml

# Under any orchestrator the app does not own its own lifetime: this hides the
# sidebar's Quit link, turns /quit into an explanation rather than a shutdown,
# and clears the pidfiles a dead container left behind on the volume. One click
# in the sidebar should not stop the server under the two people using it.
ENV BELLWETHER_MANAGED=1

VOLUME ["/data"]
EXPOSE 8787

# /healthz is the one route that needs no session, so it is the only sensible
# container health check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "prospect.webapp:app", \
     "--host", "0.0.0.0", "--port", "8787", \
     "--workers", "2", "--log-level", "warning"]
