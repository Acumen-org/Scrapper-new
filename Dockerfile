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
 && apt-get install -y --no-install-recommends tini curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so code edits do not invalidate the wheel layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY prospect/ ./prospect/
COPY scripts/ ./scripts/
COPY config/ ./config/

# Runs unprivileged. The volume is chowned to this uid by the compose file.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bellwether
USER 10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BELLWETHER_DATA=/data \
    BELLWETHER_DB=/data/prospect.db \
    BELLWETHER_HTTPS=1

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
