#!/usr/bin/env bash
# Deploy the newest CI-approved commit, if there is one. Meant for a systemd
# timer on the Nomad client; safe to run every few minutes forever.
#
# Why it watches a "release" branch rather than "main": CI fast-forwards
# `release` only after the gate passes, so this can never deploy a commit whose
# tests failed. Watching `main` would race CI and occasionally ship a red build.
#
# Why it builds locally rather than pulling from GHCR: the repo is private, so
# the image is too, and the jobspec carries no registry credentials. Building on
# the box needs no secret anywhere. The tag is the commit sha, never :latest,
# because auto_revert needs distinct tags to have something to revert to.
#
#   ./autodeploy.sh              deploy if release moved
#   ./autodeploy.sh --dry-run    say what it would do, change nothing
#   ./autodeploy.sh --force      rebuild and redeploy the current release sha
set -euo pipefail

REPO="${BELLWETHER_REPO:-/opt/bellwether}"
HOSTNAME_VAR="${BELLWETHER_HOST:-bellwether.pmx.acumen-strategy.com}"
BRANCH="${BELLWETHER_BRANCH:-release}"
STATE="${BELLWETHER_STATE:-/var/lib/bellwether/deployed_sha}"
LOCK="/var/lock/bellwether-deploy.lock"
DRY=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
    *) echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

log() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# One deploy at a time. Two overlapping runs would mean two docker builds and,
# worse, two `nomad job run` calls racing each other.
exec 9>"$LOCK"
if ! flock -n 9; then log "another deploy is already running; leaving it alone"; exit 0; fi

cd "$REPO"

# A dirty tree means somebody edited on the server. Refuse rather than silently
# building something that is not in git and cannot be rolled back to.
if [ -n "$(git status --porcelain)" ]; then
  log "REFUSING: working tree at $REPO has local changes. Commit or discard them."
  exit 1
fi

git fetch --quiet origin "$BRANCH"
TARGET="$(git rev-parse --short=12 "origin/$BRANCH")"
CURRENT="$(cat "$STATE" 2>/dev/null || echo none)"

if [ "$TARGET" = "$CURRENT" ] && [ "$FORCE" -eq 0 ]; then
  log "up to date at $TARGET"
  exit 0
fi

log "deploying $TARGET (was $CURRENT)"
if [ "$DRY" -eq 1 ]; then
  log "dry run: would check out $TARGET, build bellwether:$TARGET, and run the job"
  exit 0
fi

git checkout --quiet --detach "origin/$BRANCH"

IMAGE="bellwether:$TARGET"
log "building $IMAGE"
docker build --quiet -t "$IMAGE" . >/dev/null

log "running the Nomad job"
nomad job run \
  -var "image=$IMAGE" \
  -var "hostname=$HOSTNAME_VAR" \
  bellwether.nomad.hcl

# Only record success after Nomad accepted the job. If the new allocation turns
# out unhealthy, auto_revert puts the old one back; the recorded sha then points
# at a version that is not running, so the next run redeploys and the health
# check gets another chance. That is the desired behaviour: it retries rather
# than sitting silently on a failed deploy.
mkdir -p "$(dirname "$STATE")"
printf '%s\n' "$TARGET" > "$STATE"
log "deployed $TARGET"
