# Bellwether

A bellwether is a leading indicator. That is what every row in this tool is: a
filing, a hire, a firm's own words or a change that says an investment adviser
is worth calling before anyone else has noticed.

Bellwether is the intelligence platform on US registered investment advisers
behind go-to-market for Prairie Hill (PHH), AcuBooth and Glynac. It reads every
adviser in the SEC and state feeds, every person registered at them, their
brochures, their websites and public records, and turns that into ranked
product lists, people and contacts, hiring signals, and a dossier on every
firm, with Bellwether AI on top to ask questions in plain English.

## Signing in

**Sign in with Microsoft** is the main way in, using your Acumen Strategy
account. An admin sets it up once in Settings, Sign-in (the screen shows the
exact redirect address to register in Microsoft Entra). Anyone on the
always-admin list (rahul.gopan@acumen-strategy.com by default) is an admin the
first time they sign in; everyone else arrives as a user.

Password accounts still exist for anyone without Microsoft sign-in, and stay
switched on until Microsoft sign-in works, so nobody is locked out.

    python -m scripts.manage_users add jane --name "Jane Doe" --role admin
    python -m scripts.manage_users role jane owner --products PHH
    python -m scripts.manage_users list

## Seats

| Seat | Can |
| --- | --- |
| Admin | Everything, including Settings, users, every product's scoring, enrichment sources |
| Product owner | Change the scoring of the products they own (PHH, AcuBooth, Glynac); manage enrichment sources |
| User | Work the lists: statuses, owners, notes, manual levels, saved lists, exports, Bellwether AI |

Settings is visible to admins only.

## The screens

- **Home**: the universe at a glance (firms, assets, people, reachable contacts,
  signals), every product list with its score distribution and data coverage,
  what moved (people joining and leaving firms, new registrations, asset
  jumps), who is hiring, a map, how complete the data is, what is running now,
  the team pipeline, your firms and the ones you watch.
- **Bellwether AI**: ask anything about firms, people or the market. Questions
  about the universe become a search Bellwether runs itself, so every firm in
  an answer really matched; questions about one firm are answered from that
  firm's full record.
- **Product lists**: one ranked list per product, PHH Fund I, PHH 1031, PHH JV,
  AcuBooth and Glynac. No tiers: firms are ranked by score. Every score shows
  how much of it rests on data Bellwether holds; what is missing earns nothing
  and is flagged, so a firm never ranks high because something about it is
  unknown. The **Scoring** tab shows the rules in force and is an editor for
  admins and the product's owner.
- **Signals**: everything that changed at a firm on a list, newest first.
- **Firms**: every adviser, searchable and filterable by size, registration,
  list, hiring, reachability and more, with a contacts view for mail merges.
- **People**: everyone registered at an adviser firm, with role, tenure, prior
  firm, designations, disclosures, and the best email and direct line held.
- **Firm page**: the dossier. Overview and AI brief, fit for each product with
  every factor's evidence, the people (officers first) and how to reach each,
  hiring and departures year by year, contacts and where each came from,
  signals, assets, investments, technology, compliance. Status, owner, notes,
  saved lists and Bellwether AI sit beside it.
- **Enrichment** (admins and owners): every source Bellwether reads, the
  directories and websites the team adds, firm website coverage, and email
  verification.
- **Settings** (admins): users and seats, Microsoft sign-in, the AI provider,
  email verification, crawling, background jobs, system health, review queue.

**Ctrl K** (or **/**) finds any firm, person or page from anywhere.

## How firms are scored

Each product is defined in `config/products.yml`: gates a firm must pass,
disqualifiers that remove it, factors that each earn 0 to 100 points and count
for a weight (weights add to 100), and penalties. An admin or the product's
owner can change weights, levels and thresholds, switch a factor off, and add
a factor from any data field Bellwether holds, on the Scoring tab. Edits are
validated, kept with their history, stored in the database over the shipped
file, and the list is rescored within a minute.

Missing data never inflates a score. A factor whose data is not known yet
earns zero and is listed as missing, and every score carries its coverage (the
share of the weight resting on known data) and what it could reach. Lists rank
by score, then by coverage.

Factors a person can judge better than a filing (a warm introduction, a
manager search, governance fit) accept a manual level on the firm page, which
shows who set it, when, and what the filings alone said.

## Where the data comes from

| Source | What it gives |
| --- | --- |
| SEC and state adviser feeds, weekly | size, clients, advisors, custody, private funds, marketing, services, related persons, main phone, website |
| SEC individual adviser feed, weekly | every registered rep: employer, start date, prior firms, exams, designations, disclosures; the roster and hiring |
| Form ADV Schedule A and D archive | owners and officers with titles; custodians, private funds |
| Part 2A brochures | the firm's own words; emails and phones it printed |
| The firm's website (Scrapling) | team and bio pages, vCards, people, titles, emails, direct lines, reporting platform |
| Directories and websites added on Enrichment | people, firms, emails and phones, matched to firms |
| Public DNS | Microsoft 365 or Google; mail servers for verification |
| 13F filings | holdings, for talking points |

## Contacts, and what is real

Every email and phone carries its source and a confidence, and every email
its verification result. Bellwether learns each firm's address pattern from
addresses the firm published and builds a candidate for everyone else; a
candidate is always labelled as a guess. Verification asks the firm's mail
server whether it would accept that exact mailbox and a made-up one at the
same domain, without sending anything (the method of check-if-email-exists,
built in, or a Reacher server). Only a server that accepts the real address
and refuses the made-up one counts as **verified**; accept-all domains are
labelled as such and never shown as verified.

Checking mailboxes needs outbound port 25 and works best from a server with a
fixed address and matching reverse DNS. Settings, Verification shows whether
it can.

## Bellwether AI

Optional. Connect a provider in Settings, AI: Anthropic (Claude, default model
`claude-opus-5-5`), Eden AI (one key for many vendors), or any
OpenAI-compatible endpoint. It answers questions, writes firm briefs, reads
team pages the rules could not parse, and tidies titles. Everything it writes
is labelled as AI and grounded in Bellwether's own data. A daily call limit
caps the cost. Without a provider, everything else works and the AI box finds
firms by name.

## Background jobs

Everything runs by itself. The weekly SEC cycle starts as soon as a new feed is
due; every other job (brochures, websites, directories, email patterns,
verification, email platform, custodians, scores, AI briefs) works through its
backlog in short slices, best firms first, then checks back on its own
schedule. Settings, Jobs shows each one with Run now and Pause.

## Running it

One Python app on PostgreSQL (`BELLWETHER_DSN`). See [DEPLOY.md](DEPLOY.md) for
servers and [ROLLOUT.md](ROLLOUT.md) for the Nomad path. On Windows it starts at
sign-in from `Bellwether.bat /silent` and is stopped only from Quit in the app.

## Files worth knowing

| Path | What it is |
| --- | --- |
| `prospect/webapp.py` | App shell: sign-in, seats, layout, scheduler |
| `prospect/products.py` | Scoring engine; `config/products.yml` holds the default rules |
| `prospect/people.py`, `scripts/ingest_people.py` | People, prior firms, hiring and departures |
| `prospect/contacts.py` | Every email and phone, with source, confidence and verification |
| `prospect/crawl.py`, `prospect/harvest.py`, `scripts/web_enrich.py` | Website reading on Scrapling |
| `prospect/directory.py`, `scripts/crawl_directories.py` | Directories and websites added on Enrichment |
| `prospect/verify.py`, `scripts/verify_emails.py` | Email verification (built-in SMTP check or Reacher) |
| `prospect/ai.py`, `prospect/assistant.py` | Bellwether AI |
| `prospect/jobs.py`, `scripts/autopilot.py` | Background jobs and the worker that runs them |
| `prospect/static/` | Stylesheet, scripts, and the thinking-orbs engine (MIT) |
| `data/snapshots/` | Immutable raw SEC captures, content addressed |

The Python package is still named `prospect/`. Renaming it would be churn for
no visible gain.
