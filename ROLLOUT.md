# Getting Bellwether from this PC onto Nomad, in order

DEPLOY.md explains the hosting options and what each one costs you. This is the
sequence: what to do, in what order, and what has to be true before the next step
is worth attempting. It assumes Option D, Nomad, and it assumes nobody is using
the app yet.

The order is not arbitrary. Every stage ends in something you can check, and the
checks are there because each stage can fail in a way the next stage hides.

---

## Stage 0 — The container bugs, which are already fixed

The image had never actually been run. Building it and starting it turned up
five faults, every one of which affected `docker compose` exactly as much as
Nomad. They are fixed and the fixes are verified; this section exists so that
nobody re-introduces one, and so the checks at the end stay in the routine.

| What was wrong | What it did | Fix |
| --- | --- | --- |
| `python-multipart` missing from `requirements.txt` | **The server did not start at all.** FastAPI needs it for `Form(...)`, which is every POST including the login, and its absence is an import error rather than a runtime one. Present in the Windows environment, so nothing local ever showed it. | Pinned at `0.0.32` |
| `/data` never created or chowned in the image | Docker seeds a fresh named volume from the image's mount point, ownership included. With no `/data`, it invented one owned by root, and the app — uid 10001 — could not write its database, accounts, or session key. | `mkdir -p /data && chown 10001:10001 /data` before `VOLUME` |
| `users.yml` lived in the image at `/app/config/` | `COPY` left `config/` root-owned, so creating an account failed outright. Nobody could sign in. | `BELLWETHER_USERS=/data/users.yml` |
| Even once writable, accounts were inside the image | The image is rebuilt on every deploy. Accounts made on Monday are gone on Tuesday, locking everyone out of a running server. | Same — accounts now on the volume |
| `config/` was root-owned | The weekly cycle calls `scripts.build_cusip_map`, which rewrites `config/target_securities.yml`. The first CUSIP refresh after a deploy would die on a permission error, quietly, in a background job. | `chown -R 10001:10001 /app/config` |
| `BELLWETHER_MANAGED` never set | The sidebar's Quit button stops the server for everyone. Under an orchestrator it restarts seconds later, which reads as a crash nobody can explain. | Set in the Dockerfile |

Note the shape of the first two: both are invisible on Windows and fatal in a
container. That is the category of bug this stage exists to catch, and the reason
the checks below are worth keeping rather than running once.

**The check.** This ran clean on 2026-08-27 and should be repeated whenever the
Dockerfile or requirements change:

```bash
docker build -t bellwether:test .
docker volume create bellwether-test

# accounts are written to the volume, and survive the container that wrote them
docker run --rm -v bellwether-test:/data bellwether:test python -c \
  "from prospect import auth; auth.save_users({'probe': {'name':'probe','password_hash':auth.hash_password('x'*12)}}); print(auth.USERS_FILE)"
docker run --rm -v bellwether-test:/data bellwether:test python -m scripts.manage_users list

# config/ is writable, because the weekly cycle rewrites it
docker run --rm bellwether:test sh -c 'touch /app/config/probe && echo config writable'

# and it actually boots
docker run -d --name bw-test -v bellwether-test:/data -p 18787:8787 \
  -e BELLWETHER_SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))") \
  -e BELLWETHER_CONTACT=contact@acumen-strategy.com bellwether:test
curl -s localhost:18787/healthz            # {"ok":true,"app":"Bellwether"}
curl -s -o /dev/null -w '%{http_code}\n' localhost:18787/   # 303 to /login

docker rm -f bw-test && docker volume rm bellwether-test
```

If any of it fails, stop. Nothing downstream is worth doing.

---

## Stage 1 — Code into GitHub, image into a registry

`gh` and `docker` are both installed on this machine now, so this is one
sitting. (DEPLOY.md still says `gh` is not installed. It is; that line is stale.)

Keep the repository **private**. The scoring logic is the part that took longest
to get right and it is a competitive asset, not a secret in the cryptographic
sense — but nothing in it needs to be public either.

```bash
gh repo create bellwether --private --source=. --remote=origin --push
```

Before that first push, confirm nothing large or private slipped in:

```bash
git ls-files | wc -l                    # a few dozen files, not thousands
git count-objects -vH | grep size-pack  # single-digit MB
git ls-files | grep -E "\.db|^data/"    # NO output
```

Then the registry. GHCR is the path of least resistance next to a private GitHub
repo — same account, same permissions, no new bill:

```bash
echo $CR_PAT | docker login ghcr.io -u <your-github-user> --password-stdin
docker build -t ghcr.io/<org>/bellwether:$(date +%F) .
docker push  ghcr.io/<org>/bellwether:$(date +%F)
```

**Date the tag. Never `:latest`.** Nomad's `auto_revert` has nothing to revert to
when every build shares a name, and you cannot tell from `nomad status` which
build is actually running.

**Check.** `docker pull` the tag from a machine that has never built it.

---

## Stage 2 — Prepare the Nomad client

Nothing here involves Bellwether. Do it once and it stays done.

1. **CNI plugins** on the client. The job uses bridge networking so Caddy can
   reach the app on localhost and the app is never published to the host.
2. **Two host volumes** in `client.hcl`, then restart the client:

```hcl
client {
  host_volume "bellwether_data"  { path = "/opt/bellwether/data" }
  host_volume "bellwether_caddy" { path = "/opt/bellwether/caddy" }
}
```

3. **Ownership.** The app image runs as uid 10001, the Caddy image as uid 1000:

```bash
mkdir -p /opt/bellwether/data /opt/bellwether/caddy
chown -R 10001:10001 /opt/bellwether/data
chown -R 1000:1000   /opt/bellwether/caddy
```

4. **Disk.** 50GB if you are copying the brochure cache, 20GB if not, growing
   about 0.75GB a year as weekly snapshots accumulate.
5. **Firewall to 80, 443 and SSH.** The job maps the app to a random host port
   so Nomad's health check has an address; that port should not be reachable
   from outside the box.
6. **DNS.** An A record for `bellwether.acumen-strategy.com` at the client's IP,
   in place *before* the first deploy — Caddy will try to get a certificate
   immediately and Let's Encrypt rate-limits repeated failures.

**Check.** `nomad node status -self` lists both host volumes.

---

## Stage 3 — Secrets, then first deploy with an empty database

```bash
nomad var put nomad/jobs/bellwether \
  secret=$(python3 -c "import secrets; print(secrets.token_hex(32))") \
  contact=contact@acumen-strategy.com
```

`contact` is not decoration. The SEC blocks traffic without a real User-Agent
address, so it has to be a mailbox somebody reads.

```bash
nomad job run \
  -var image=ghcr.io/<org>/bellwether:$(date +%F) \
  -var hostname=bellwether.acumen-strategy.com \
  bellwether.nomad.hcl
```

Deploy against an empty volume first, deliberately. If it comes up healthy with
nothing in it, then anything that breaks in Stage 4 is the data, not the job.

**Check.** `nomad job status bellwether` shows the allocation healthy, and
`https://bellwether.acumen-strategy.com/healthz` returns `{"ok": true}` over a
valid certificate.

---

## Stage 4 — Move the real data

3.4GB minimum. Do it with the job stopped so nothing is mid-write, and quit
Bellwether on this PC first for the same reason.

```bash
nomad job stop bellwether

scp prospect.db root@<client>:/opt/bellwether/data/
scp -r data/snapshots root@<client>:/opt/bellwether/data/
ssh root@<client> chown -R 10001:10001 /opt/bellwether/data

nomad job start bellwether
```

Skip `data/brochures/` (5.6GB). It is refetchable and the coverage job refills
it over a few days; copying it is the difference between a 20GB disk and a 50GB
one. Skip `data/web_cache/` entirely — it rebuilds as the crawler runs.

**Check.** The inbox loads with real firms in it, and `/health` shows no flags.

---

## Stage 5 — Accounts, and the first real sign-in

```bash
nomad alloc exec -task app -job bellwether \
  python -m scripts.manage_users add rahul --name "Rahul Gopan"
nomad alloc exec -task app -job bellwether \
  python -m scripts.manage_users add alisa --name "Alisa Chen"
```

The password is typed at a prompt, never passed on a command line, and only a
PBKDF2-SHA256 hash is stored. `nomad alloc exec` allocates a TTY by default, so
the prompt works.

Sign-in is not only a lock. The name on the account is what fills in who owns a
firm and who cleared a review, so a shared queue stays honest about who did what.

**Check.** Redeploy the job and sign in again. If the accounts survive, Stage 0
worked. This is the single most important check in the whole sequence, because
the failure mode is silent until the day it locks all three of you out.

---

## Stage 6 — The things that make it stay usable

Everything above gets it running once. These are what keep it running.

### Backups — deliberately deferred

Decided on 2026-08-27: not being set up yet. Written down rather than left
implicit, because "we have not got to it" and "we decided we do not need it" look
identical six months later and only one of them is a decision.

What is being deferred is offsite backup. DEPLOY.md's command writes `backup.db`
onto the same volume as the database, which survives a bad write but not a lost
disk. The exposure until this is picked up: losing the machine means rebuilding
the database from the snapshots, which is hours of SEC fetching, and losing
anything not derivable from them — ownership, cleared reviews, accounts.

Revisit when the queue holds review decisions somebody would not want to redo.

### The QA gate as a merge gate

`python -m scripts.qa_smoke` already checks the login wall, every route, every
write path, latency, and behaviour under concurrent load while the ingester
writes. It exits nonzero on failure, so it is usable as CI unchanged. There is no
CI configured today; a GitHub Actions workflow that builds the image and runs the
gate on every pull request is the highest-value thing left in this repo.

### Deploys

```bash
git pull && python -m scripts.qa_smoke      # must print ALL CHECKS PASS
docker build -t ghcr.io/<org>/bellwether:$(date +%F) . && docker push ...
nomad job run -var image=... -var hostname=... bellwether.nomad.hcl
```

The job stops the old allocation before starting the new one, which is the only
safe order when both would open the same SQLite file. That means a few seconds of
downtime on every deploy. That is the correct trade and not something to tune
away with a canary.

### Watching the weekly cycle

It runs unattended and fails quietly if the SEC changes a feed. `/health` carries
the flags; look at it on Mondays. `python -m scripts.prune` shows what has stopped
earning its disk, and `--apply` reclaims it.

---

## What this plan does not give you

Worth saying plainly, so nobody discovers it at a bad moment.

**No high availability.** One allocation, one client, one SQLite file, pinned by
a host volume. If that machine dies, Bellwether is down until it comes back or
you restore a backup somewhere else. This is a deliberate consequence of the data
model, not an oversight — and for three people it is the right trade.

**Nomad buys you very little here.** If you are standing up a cluster *for* this,
Option A in DEPLOY.md is the same two containers with a fraction of the moving
parts. Nomad is worth it only if the cluster already exists.

**One Bellwether per client.** Caddy binds ports 80 and 443 statically, so a
second job wanting them will not place.

**Nothing is scheduled to verify the backups restore.** Until you have restored
one into a scratch volume and signed in against it, you have a backup procedure,
not a backup.
