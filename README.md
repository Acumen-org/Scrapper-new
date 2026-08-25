# Bellwether

A bellwether is a leading indicator. That is what every row in this tool is: a
change in a public SEC filing that says a registered investment adviser is worth
calling before anyone else has noticed.

Bellwether reads the adviser universe from SEC sources, scores it against two
products, and produces a queue that three people work each morning. It is not a
report. One Python process, one SQLite file (`prospect.db`), no third-party
enrichment service, and no data leaving the machine except requests to the SEC
and to advisers' own public websites.

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

## The views (sidebar, left)

- **Trigger inbox**: the 9am screen. Everything that changed, highest priority
  first, one actionable line per row. Done / Snooze / Dismiss on each. Negative
  priority in dark red is a disqualifier (firm left Schwab), not a lead.
- **Firm list**: all 13,720 in-band advisers, SEC and state registered. Filter
  by state, AUM band, real
  estate segment, open triggers, status, owner. Export the filtered view as CSV
  (shaped for manual Twenty import; carries phone, filed email, status, owner).
- **Working lists**: the ranked outputs. Tier A top 100 (PHH), the intersection
  (both gates cleared, Alisa's list), tier C top 100 (AcuBooth), and competitor
  sponsors with Form D raise progress.
- **Review queue**: uncertain 13F links (Same firm / Different firm) and
  brochure negations (Real negation / Tag stands). Ranked by impact; the tail
  can sit unreviewed forever without blocking anything.
- **Pipeline health**: last run of every stage, snapshot inventory, row deltas,
  parse failure rates, coverage, CUSIP map age. The **Run weekly cycle now**
  button runs the whole pull-diff-rescore chain in the background, and the
  **Autopilot** panel runs long jobs (brochure coverage, contact extraction,
  website enrichment, flagged-firm refresh, email verification, CUSIP re-verify),
  each with why it exists, Start and Pause, and a live progress bar.
- **Outreach**: one row per decision maker with their best email and phone,
  real addresses and pattern-inferred guesses both labelled, exportable as Excel
  for a mail merge.
- **How to use**: the operating manual as one continuous read, top to bottom,
  with a contents list on the right that tracks where you are. Every screen it
  mentions is a live link.

## Power features

- **Ctrl+K** jumps to any firm by name or CRD from anywhere.
- Inbox keyboard: **j/k** select, **d** done, **s** snooze, **x** dismiss,
  **Enter** opens the firm.
- **Watch** any firm (star on its page); open events on watched firms pin to
  the top of the inbox.
- **Saved views**: save any inbox filter combination; it appears in the sidebar.
- **Geography** (Working lists tab): state, then city, then a dinner-ready list
  of qualified firms with CSV export.
- Every firm page shows its **AUM trajectory** back to 2011 with real axes, and
  **People**: owners and executive officers with their filed titles from
  Schedule A, then registered reps from the individual feed, then anyone found
  on the firm's own website.

## Managing firms

Open any firm (click its name anywhere). You can:

- set **status** (new, working, meeting set, qualified, disqualified, customer)
  and **owner**; both become filters on the firm list and columns in the CSV
- write **notes** that persist
- click **Copy to clipboard** for a paste-ready call prep summary with every
  caveat carried inline

## Weekly rhythm

The weekly pull runs itself. A scheduler inside the app checks every half hour
whether a new SEC feed file is due (the feed publishes weekly and keeps no
archive) and runs the full cycle when it is: capture, forward triggers, rescore,
brochure slice, CUSIP re-verify when due. It catches up the moment the PC comes
back on after a missed week. Pipeline health shows its heartbeat and the last
automatic pull; the manual **Run weekly cycle now** button remains for the rare
day a pull is wanted immediately.

## Contact data, and what is real

Every in-band firm has its **main office phone** as filed on Form ADV. The
contact extraction job reads the first pages of each firm's own brochure for the
**emails and phone numbers the firm itself printed there**; on the PHH working
list roughly six firms in ten have a filed email. Everything from a filing is
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
| `prospect.db` | Everything. Back this up. |
| `data/snapshots/` | Immutable raw SEC captures, content addressed. |
| `config/*.yml` | Every tunable: weights, thresholds, bands, tickers, phrases. |

The Python package is still named `prospect/` and the database `prospect.db`.
Renaming those would be a data migration for no user-visible gain, so they stay.
