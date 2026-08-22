# Putting Bellwether on the web for the team

Two separate things, and it helps to keep them separate:

**GitHub holds the code.** That is what lets three people work on it: history,
changes, rollback. It does not run anything. GitHub Pages serves static files
only, so it cannot run Python or hold a database.

**A host runs the app.** Pick one of the three below.

The repository never contains the database, the captured SEC files, the brochure
cache, or any password. Those are 6.6GB of working data and they live only on the
machine that runs Bellwether.

---

## 1. Put the code on GitHub (do this first, whichever host you pick)

`gh` is not installed here, so create the repository in the browser: New
repository, name it `bellwether`, **Private**, and do not add a README or
.gitignore (this repo has its own).

Then, from this folder:

```bash
git remote add origin https://github.com/<your-org>/bellwether.git
git branch -M main
git push -u origin main
```

**Keep it private.** The code embeds how the scoring works, which is the part
that took the longest to get right, and the repo is the wrong place for a
competitive asset. Nothing in it is secret in the cryptographic sense, but
nothing in it needs to be public either.

Before the first push, confirm nothing large or private slipped in:

```bash
git ls-files | wc -l                  # expect a few dozen files, not thousands
git count-objects -vH | grep size-pack # expect single-digit MB
git ls-files | grep -E "\.db|^data/"   # expect NO output
```

---

## 2. Create accounts

Nobody can reach anything without signing in. On whichever machine runs
Bellwether:

```bash
python -m scripts.manage_users add rahul --name "Rahul Gopan"
python -m scripts.manage_users add alisa --name "Alisa Chen"
python -m scripts.manage_users list
```

Passwords are typed at a prompt, never passed on a command line, and only a
PBKDF2-SHA256 hash is stored. `config/users.yml` is gitignored.

Sign-in is not only a lock. The name on the account is what fills in who owns a
firm and who cleared a review, so a shared queue stays honest about who did
what.

---

## Option A: One small server, always on (recommended)

Best fit: the app is one process with one SQLite file and background jobs that
run for days. A single box with a disk matches that exactly.

A 4GB machine is plenty. Hetzner CPX21 is about EUR 8/month, DigitalOcean's
equivalent about USD 24.

```bash
# on the server
git clone https://github.com/<your-org>/bellwether.git
cd bellwether
cp .env.example .env
nano .env          # set BELLWETHER_SECRET, BELLWETHER_CONTACT, BELLWETHER_HOST
```

Generate the secret with:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Point a DNS A record at the server's IP (for example
`bellwether.acumen-strategy.com`), put that hostname in `BELLWETHER_HOST`, then:

```bash
docker compose up -d --build
docker compose exec app python -m scripts.manage_users add rahul --name "Rahul Gopan"
```

Caddy obtains and renews the HTTPS certificate by itself. Bellwether is not
published on a host port, so the only route in is through Caddy over TLS.

### Getting the data there

The database is regenerable but rebuilding it takes hours of SEC fetching. Copy
it instead. From this Windows machine, with Bellwether quit so the file is not
mid-write:

```bash
scp prospect.db root@<server>:/tmp/
scp -r data/snapshots root@<server>:/tmp/
```

Then place them on the volume:

```bash
docker compose cp /tmp/prospect.db app:/data/prospect.db
docker compose cp /tmp/snapshots app:/data/snapshots
docker compose restart app
```

The brochure cache (3.4GB) does not need copying. It only saves re-downloading;
the coverage job refetches anything missing, and the extraction job reads
whatever is there.

---

## Option B: Keep it on this PC, private network

Nothing goes on the public internet. Install Tailscale on your PC and on your
teammates' machines, all signed into the same Tailscale account. They then reach
`http://<your-pc-name>:8787` as if on the same LAN.

Nothing else changes: Bellwether already starts at logon and is already behind a
login. Set `BELLWETHER_HTTPS=` empty (Tailscale traffic is encrypted, but the
connection your browser sees is plain http, and a Secure cookie would not be
sent over it).

The catch is real: when your PC sleeps, reboots, or you take it out of the
office, nobody else can use Bellwether. Fine for a trial, thin for daily work.

---

## Option C: Managed container host

Fly.io or Render will build the Dockerfile on every push. Least server
maintenance, but understand the tradeoff before choosing it: these platforms
restart and relocate containers freely, and Bellwether keeps a 2.2GB SQLite file
and a brochure cache on local disk. A relocation mid-write is exactly the risk
this design does not want, and every redeploy kills the background jobs.

If you go this way, attach a persistent volume (Fly: `fly volumes create
bellwether_data --size 20`), mount it at `/data`, and run a single machine, never
two. Two machines writing one SQLite file over a network filesystem will corrupt
it.

---

## Working on it together

```bash
git checkout -b whatever-you-are-changing
# edit, then:
python -m scripts.qa_smoke        # must print ALL CHECKS PASS
git commit -am "what changed and why"
git push -u origin whatever-you-are-changing
```

Open a pull request, have someone read it, merge to `main`. On the server:

```bash
git pull && docker compose up -d --build
```

Run the QA gate before every merge. It checks the login wall, every route, every
write path, latency, and behaviour under concurrent load while the ingester is
writing. It exits nonzero on any failure, which makes it usable as a CI step.

## Backups

One file matters: `prospect.db`. Everything else is either regenerable or
already on disk as an immutable snapshot.

```bash
docker compose exec app sh -c \
  'sqlite3 /data/prospect.db ".backup /data/backup.db"' \
  && docker compose cp app:/data/backup.db ./backup-$(date +%F).db
```

Use `.backup` rather than copying the file: a plain copy of a live SQLite
database can capture a half-written transaction.
