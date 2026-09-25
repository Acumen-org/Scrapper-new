# Bellwether

A bellwether is a leading indicator. That is what every row in this tool is: a
filing, a firm's own words or a change that says an investment adviser is worth
calling before anyone else has noticed.

Bellwether reads the adviser universe from SEC sources, scores it against five
product lists across PHH, AcuBooth and Glynac, and gives the team who to call,
why, and what to say. One Python app on PostgreSQL, no paid or third-party
enrichment service, and no data leaving the machine except requests to the
SEC, to advisers' own public websites and to public DNS.

## Signing in

Everyone has a named account. Nothing is reachable without one, and the name on
the account is what fills in who owns a firm and who cleared a review, so a
shared queue stays honest about who did what.

    python -m scripts.manage_users add alisa --name "Alisa Chen"
    python -m scripts.manage_users list

Passwords are typed at a prompt and stored only as PBKDF2-SHA256 hashes.
`config/users.yml` is gitignored and never leaves the machine.

To put this on a server for the team, see [DEPLOY.md](DEPLOY.md).

## Starting and stopping

There is nothing to start. Bellwether launches itself when you sign in to
Windows, with no console window and no browser tab, so it is simply already
running when you sit down.

- **Open it**: the **Bellwether** shortcut on your desktop. If it is not running
  for any reason, that shortcut starts it first.
- **Stop it**: **Quit Bellwether** at the bottom of the sidebar. That is the only
  way to stop it, and it is rarely needed. Quitting also stops background jobs,
  which resume from where they left off.
- **Turn off starting at sign in**: Task Manager, Startup apps, switch Bellwether
  off. The entry is a shortcut in your own Startup folder, so deleting it works
  too.

There is deliberately no stop script. A second batch file for shutting something
down is a thing to remember, and it does not belong in a tool people use daily.

## What it does

Bellwether is the data and intelligence layer for go-to-market across three
businesses: Prairie Hill (PHH), AcuBooth and Glynac. It reads every adviser in
the SEC and state feeds, adds what each firm says about itself (its Part 2A
brochure, its website, its public mail records) and turns that into:

- **Product lists**: one ranked list per product, each firm with a tier, a score
  out of 100 and the reasons it earned it, in the firm's own words.
- **Signals**: what changed this week at a firm on any list.
- **Firm pages**: everything needed to decide whether to call and what to say.

## The screens

- **Home**: how every list stands (tier counts that open the list already
  filtered), the best unclaimed firms to call first on each list with the reason
  and the newest signal, your own firms, and firms you watch.
- **Product lists** (sidebar): PHH Fund I, PHH 1031, PHH JV, AcuBooth and Glynac.
  Each has three views: *Ranked* (filter by tier, state, owner, status, new
  signal, reachability; sort; save the view; export the list or its contacts),
  *Disqualified* (removed, with why) and *How it is scored* (the product's
  scoring table, rendered from the config, so the rules on screen are the rules
  in force).
- **Signals**: every trigger at a firm on a list, newest first, with the firm's
  best tier beside it. Done, Snooze, Dismiss; keyboard j/k, d, s, x, Enter.
- **Firms**: every adviser, searchable by name, city or CRD and filterable by
  size, registration, list and tier, with a Contacts view for mail merges.
- **Saved lists**: firm lists you build by hand, and saved views.
- **System**: data freshness, coverage of the lists by each enrichment,
  background jobs with Start and Pause, run history and the review queue.

**Ctrl K** (or **/**) finds any firm or page from anywhere. Clicking any row
opens the firm.

## How firms are scored

Every product is defined in `config/products.yml` in one format: gates a firm
must pass, disqualifiers that remove it, criteria that each earn 0 to 100 points
and count for a weight (weights add to 100), penalties, and tiers that say what
a score means. The logic that decides each criterion's level is one small
function per criterion in `prospect/products.py`, and every level it sets comes
with the evidence that set it. The same breakdown appears on every firm page.

Criteria that a person can judge better than a filing (a warm introduction, an
active manager search, governance fit) accept a manual level on the firm page.
It replaces the computed level for that firm only, shows who set it and when,
and keeps what the filings alone would have said. Status matters too: a
meeting or a customer raises the relationship points on every list.

The sources behind the scores:

| Source | What it gives |
| --- | --- |
| SEC and state adviser feeds, weekly | size, client mix, advisors, custody, private funds, marketing answers (Item 5.L), services (5.G), related persons (7.A), social media listed (1.I) |
| Schedule D archive and Schedule A | fund details, custodians, officers and their titles |
| Part 2A brochures | the firm's own language: covered calls, alternatives, real estate, 1031, model portfolios, investment committee, reporting platform |
| The firm's website | people and contacts, the client login that names its reporting platform, whether it publishes |
| Public DNS mail records | Microsoft 365 or Google |
| 13F filings | target holdings for talking points |

`python -m scripts.score_products` rescores everything in seconds; the weekly
cycle runs it, and so does **Recompute scores** on System.

## Working a firm

On any firm page: set a **status** and **owner** (blank claims it for you), write
**notes**, **watch** it (its signals then lead Home and Signals), add it to a
saved list, and **Copy call prep** for a paste-ready summary carrying the tier,
the reasons, what changed, the firm's own words and how to reach them.

## Weekly rhythm

The weekly pull runs itself. A scheduler inside the app checks every half hour
whether a new SEC feed file is due and runs the full cycle when it is: capture,
ADV answers, triggers, 13F match, email platforms, scoring, a slice of brochures
and websites, and a final rescore so what they read is already in the lists. It
catches up the moment the machine comes back after a missed week. Longer work
(brochure coverage, re-tagging, email platforms, websites, contacts) runs as
background jobs on System, best-priority firms first: tier A on any list, then
tier B, and so on.

## Contact data, and what is real

Every firm has its **main office phone** as filed on Form ADV. The
contact extraction job reads the first pages of each firm's own brochure for the
**emails and phone numbers the firm itself printed there**; on the product lists
roughly six firms in ten with a brochure have a filed email. Everything from a filing is
marked **filed**. Pattern-guessed emails still exist as a labelled fallback,
generated only against the firm's own mail domain (from its brochure when
possible), never against social or freemail domains, and a guess on an
accept-all domain can never show as verified.

Guessed addresses get a free local check: valid syntax, and whether the domain
publishes a mail server (a DNS lookup done here, no account and no third party).
That can prove an address is worthless; it never claims a mailbox exists, since
only sending mail proves that. Nothing in Bellwether costs money to run.

## Reading the numbers honestly

Hover any dotted-underline figure for its caveat. The two that matter most:
Schwab share is of REPORTED custodians only (10%+ holders), and it flags the
late-2026 institutional opportunity, never accounts sellable today. Estimated
client size is a client-level figure, biased high as an account proxy.

## Files worth knowing

| Path | What it is |
| --- | --- |
| `Bellwether.bat` | The launcher. `/silent` starts it without opening a browser. |
| `scripts/launch.vbs` | Runs the launcher with no window at all, even briefly. |
| `assets/bellwether.ico` | App icon, regenerate with `python -m scripts.make_icon`. |
| PostgreSQL (`BELLWETHER_DSN`) | Everything. Back this up. |
| `data/snapshots/` | Immutable raw SEC captures, content addressed. |
| `config/products.yml` | Every product list: gates, weights, levels, tiers. |
| `config/*.yml` | The other tunables: triggers, tickers, brochure phrases. |

The Python package is still named `prospect/` and the database `prospect.db`.
Renaming those would be a data migration for no user-visible gain, so they stay.
