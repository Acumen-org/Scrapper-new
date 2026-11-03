# Outreach sandbox: the Bellwether side

Written 2026-11-02 from Rahul's brief (`Tech Projects/cadence-outreach-sandbox-brief.md`), updated
2026-11-03 with Rahul's decisions (`Tech Projects/rahul-decisions-2026-11-03.md`, summarised under
Decisions at the end; they override anything else here). Rahul gave the go-ahead on 3 November, and
the whole plan is built in one pass on the local branch `outreach-sandbox`. Cadence's plan
(`Sequencer/docs/outreach-sandbox-plan.md`) owns the push API. Its section 14, version 1 of the
contract (14.1 to 14.14), is copied below byte for byte, by script, as Shared contract with
Bellwether, and this plan follows it.

Bellwether's part of the brief is requirement 5: users push **contacts, not firms**, one or many,
into Cadence's outreach sandbox, and Bellwether shows per contact whether it went to Cadence and
whether it later moved to the CRM. Everything else in the brief (sandbox, campaigns, Move to CRM,
Twilio, the post-call form, Graph email tracking, analytics) is Cadence's.

## What Bellwether has today, and what that means for the push

| Fact in the code | Consequence |
| --- | --- |
| A contact is a row of `people_index` (`prospect/people_index.py`), keyed by `(crd, person_key)`. `person_key` is `i:<indvl_pk>` for SEC-registered reps and `n:<first last>` for Schedule A officers and website people. A person at two firms has two rows. | The Bellwether contact id is `<crd>:<person_key>`. Push tracking keys on `(crd, person_key)`. |
| `people_index` is rebuilt in full daily and swapped in (`DROP` then `RENAME`), and refreshed per firm every 30 minutes. `person` and `person_employment` are replaced whole by each weekly people load. | Push state cannot live as columns on those tables. It needs its own table, joined at read time. |
| Emails, phones and LinkedIn live in `contact_point` (`prospect/contacts.py`). Screens read the `usable_contact_point` view, which never holds an unconfirmed guess. | The payload is built from `usable_contact_point` at push time, not from the index's copy, which can be 30 minutes old. Guesses can never be pushed. |
| There is no per-person page. A People row links to `/firm/{crd}#people`, where `_person_card` in `prospect/firm_view.py` renders the person. | "The contact page" in this plan is that person card on the firm page's People & contacts tab. |
| No opt-out, do-not-contact or unsubscribe field exists anywhere in `prospect/` (searched). | Compliance needs a new suppression table. |
| Ownership exists only per firm: `firm_status.owner` stores a display name (`current_owner()` in `prospect/webapp.py`). | Contact owners are sent by email address (`pushedBy.email`, `assignTo.email`), taken from `app_user`, never from `firm_status.owner`. |
| Three seats (`prospect/users.py`: admin, owner, user). `require_login` in `prospect/webapp.py` denies by default, returns JSON 401 under `/api/`, and makes anything under `/settings` admin-only. The session cookie is `SameSite=Strict`, which is the app's CSRF defence for POSTs. | New JSON routes go under `/api/cadence/`, admin screens under `/settings/cadence`, and both are protected without extra code. Every signed-in seat may push (decision 16), so the push routes need only the sign-in. |
| There is no migration tool. Each module owns a `SCHEMA` of `CREATE TABLE IF NOT EXISTS`, run at startup by `_init_optional()` in `prospect/webapp.py`. Columns are added with `db.add_column`. GETs never issue DDL. | The new tables ship as `prospect/cadence.py`'s `SCHEMA` and register in that startup list. |
| Integrations are configured in `prospect/settings.py` `SPECS`. Secrets are encrypted with a key derived from `BELLWETHER_SECRET`, and an environment variable wins when it is non-empty. | The Cadence switch and caps are new Specs. There is no push-permission setting (decision 16). The URL and key are not: the contract (14.1, 14.2) has Bellwether read them from `CADENCE_API_URL` and `CADENCE_API_KEY` and never store the key in a database, so Settings › Cadence only shows whether each is set. |
| Background work is the `JOBS` registry in `prospect/jobs.py`, run in lanes (`mail`, `web`, `search`, `data`), with `RETRY_MINUTES = (15, 60, 180, 480)` for job reruns. | Status sync, and resending what a dead process left, are one new job in a new `cadence` lane. Pushes do not use `RETRY_MINUTES`; they retry on the contract's schedule (14.10). |

## What changes, file by file

| Area | File | Change |
| --- | --- | --- |
| Core | `prospect/cadence.py` (new) | Schema, payload builder with the 14.5 normalisation, block rules, the CRM check before a push (14.14), Bellwether-side dedupe and claim, HTTP client, pacing, result recording, status sync, suppression checks. |
| Routes | `prospect/cadence_view.py` (new router, registered with the other `app.include_router` calls in `prospect/webapp.py`) | The routes listed under UI, Routes. |
| Startup | `prospect/webapp.py` | Add `"cadence"` to the `_init_optional` module list in `_init_once`. |
| People list | `prospect/people_view.py` | Selection column, selection bar, Cadence status line in the Person cell, a Cadence filter under More filters, `cadence_status` and `cadence_owner` columns in `/people/export.csv`. |
| Contact (person card) | `prospect/firm_view.py` | Push button or status line on each `_person_card`; select-many on the People & contacts tab; Do not contact action. |
| Front end | `prospect/static/app.js`, `prospect/static/app.css` | Selection state, the confirm dialog (native `<dialog>`, like the firm AI dialog), the per-contact result list, polling a batch, the by-id status read when the People & contacts tab opens. |
| Settings | `prospect/settings.py`, `prospect/settings_view.py` | A `cadence` group of Specs (switch, caps) and a Settings › Cadence screen. The URL and key come from the environment only (see Credentials, config and deploy). |
| Job | `prospect/jobs.py`, `scripts/cadence_sync.py` (new) | `"cadence"` added to `LANES`, and `Job("cadence_sync", ...)` in it: status sync every 10 minutes, resend requests a dead process left, unstick stale claims, lift holds. |
| Health | `prospect/health.py` | Entries in `alerts()` when Cadence refuses the key (`401`), reports the integration switched off (`503 integration_disabled`), a request failed for a reason retrying cannot fix (`409 idempotency_conflict`, `400`, `413`, `415`, `422`, a second `500`), pushes have waited over an hour, or status sync has failed for two hours. |
| Diagnostics | `scripts/deployment_diagnostics.py` | Counts by push state, last sync, last error class, whether `CADENCE_API_URL` and `CADENCE_API_KEY` are set. No contact values and never the key. |
| Deploy | `nomad/bellwether.nomad.hcl`, `.env.example`, `DEPLOY.md`, `README.md` | Env names (see Credentials, config and deploy), the new files in "Files worth knowing". |
| Tests | `scripts/fake_cadence.py` (new), `scripts/qa_requirements.py`, `scripts/qa_pg.py`, `scripts/qa_smoke.py`, `scripts/qa_html.py`, `scripts/qa_visual.py` | See Tests. |

`people_index` is not changed, so `BUILD_VERSION` stays at 2 and no rebuild is triggered.

## UI

### Who may push

- Everyone can push (decision 16): every seat (admin, product owner, user), with no setting to
  narrow it and nothing extra for admins. That includes pushing again a contact Cadence removed.
- Admin-only, because they are settings rather than pushing: Settings › Cadence, lifting a
  do-not-contact, the full push log, and Retry for failed requests (anyone can push a failed
  contact again from People or the card).
- Anyone can mark a person Do not contact.
- Pushing needs an email address on the pusher's Bellwether account, because `pushedBy.email` is
  required (14.4). Without one the dialog says so and cannot send.
- When `CADENCE_API_URL` or `CADENCE_API_KEY` is unset, or `cadence.enabled` is off, the Push
  button stays visible and its dialog explains that Cadence is not connected. Nothing is sent and
  nothing else on the page changes.

### People list (`/people`)

- A first column of checkboxes. Each is labelled "Select <name>". The cell stops the row click,
  because every row is a `tr.go` that opens the firm. A header checkbox selects the page.
- Once one row is selected, a selection bar sticks to the bottom of the viewport: "3 selected ·
  Push to Cadence · Clear". It uses the shared overlay shadow that DESIGN.md reserves for bulk
  actions.
- When the whole page is selected, the bar offers "Select all 1,234 matching". The server resolves
  that from the same filter (`_where()` in `people_view.py`), capped at `cadence.max_per_push`
  (default 200). Above the cap, the bar says so and asks the user to narrow the filter.
- Selection does not carry across pages. "All matching" covers that case.
- Under the person's title, a quiet line shows state: "In Cadence · Ana Ruiz", "In Cadence ·
  unclaimed", "Moved to CRM", "Already in the CRM", "Opted out (Cadence)", "Cadence refused:
  <reason>", "Do not contact". "Opted out (Cadence)" shows whenever the person has an active
  suppression with `source='cadence'` (decision 23), whether it came from the status sync, a by-id
  refresh or a `suppressed` refusal. Neutral chips, with readable red text only for refused or
  failed. No coloured dots.
- A Cadence filter under More filters: Any, Not pushed, In Cadence, Moved to CRM, Opted out
  (Cadence), Do not contact.
  It is an `EXISTS` on `cadence_push` by `(crd, person_key)`. That table is small and indexed, so the
  screen stays one fast read.

### Contact (person card on `/firm/{crd}#people`)

- Under the contact lines: a small "Push to Cadence" button, or the status line with who pushed and
  when, the Cadence owner (or "unclaimed"), and an "Open in CRM" link (`crmUrl`) once the contact
  is in the CRM. A person opted out in Cadence shows "Opted out (Cadence)" and no Push button
  (decision 23). The contract has no link to a sandbox contact, so v1 has no "Open in Cadence"
  link. A tooltip lists the push history from the audit table.
- A "Select" toggle on each card feeds the same selection bar, so several people at one firm go in
  one push.
- Opening the People & contacts tab reads Cadence's status for the pushed people on it, by id
  (14.12): after the page renders, it posts them to `/api/cadence/refresh`. A "Refresh" link on a
  pushed card does the same for one contact. Page GETs never call Cadence.
- A "Do not contact" action records a suppression (see Compliance) with a reason.

### Confirmation

One dialog serves every entry point:

1. Opening it calls `POST /api/cadence/preview` with the selection. The preview writes nothing in
   Bellwether or Cadence. It runs Bellwether's block rules, then sends the contacts that pass them
   to Cadence's check endpoint (`contacts/check`, 14.14) in one request (the selection is at most
   200, under the check's 500), with the same body a push would send.
2. It shows "Push 12 contacts to Cadence" and two groups. **Will send (10)** lists name, firm, and
   the email and phone that will go, with their status ("Verified", "Published", "Main line"), and
   for a contact the check found in the sandbox, "Already in Cadence's sandbox (owner: X); the push
   fills its empty fields". **Skipped (2)** lists each contact with its reason: already in Cadence
   (by whom, when), do not contact, opted out (Cadence), no email and no phone, firm marked
   Customer, same person already pushed from another firm, and, from the check:
   - **"Already in the CRM · matched on email"** (or phone, or LinkedIn), with the CRM owner and an
     "Open in CRM" link, for every check result `already_in_crm` (decision 17). These contacts are
     not sent.
   - "Cadence would refuse: <reason in words>" for every check result `rejected`. Sending them
     would only bring back the same refusal, so they are not sent either.
3. If the check cannot be made:
   - `401`, or `503 integration_disabled`: the dialog says Cadence refused the key or has the
     integration switched off, and the Push button is disabled.
   - A timeout, connection error, `429` or other 5xx: the dialog says "Cadence's CRM check did not
     answer, so these contacts were not checked against the CRM. Cadence still refuses anyone
     already in the CRM when the push arrives (14.6 rule 4)", and the push may go ahead. The check
     is never retried in the background.
4. Fields: Owner (default: you; any active Bellwether user with an email address, sent as
   `assignTo.email`), Product (default: the list being viewed; PHH Fund I, PHH 1031, PHH JV,
   AcuBooth, Glynac or none; sent as Field mapping › Product interest says), and an optional Note
   of up to 500 characters. ATS is not offered: a push from the ATS list goes with no product
   (decision 25). Under Owner: "If Cadence cannot give them the contact, it arrives unclaimed"
   (14.7). Changing Owner or Product runs the preview again, because the check's body changes.
5. If a firm is marked Disqualified, the dialog warns and asks for a tick before sending. A firm
   marked Customer blocks its contacts (decision 17).
6. The button reads "Push 10 to Cadence". The push route runs Bellwether's block rules again and
   leaves out the contacts that this preview's check blocked. It keeps the check's answers for 15
   minutes under the preview's id; a push without a fresh preview calls the check again first.
   The push itself still handles an `already_in_crm` result the same way if the CRM changed in
   between.

### Per-contact result

- After sending, the dialog becomes a result list, one line per contact: "Added to sandbox (owner:
  X)" or "Added to sandbox, unclaimed", "Already in sandbox (owner: X)", "Already in the CRM
  (owner: X) · matched on <field>", "Refused: <reason in words>", "Will retry: Cadence did not answer", "Failed: <reason>",
  "Skipped: <reason>". Cadence's warnings follow the line, for example "Cadence dropped the phone"
  or "Possible match in the CRM". It ends with a count line.
- Retries, and selections larger than one Cadence request, carry on in the background. The dialog
  polls `GET /api/cadence/batch/{id}` every two seconds until the batch is finished.
- When the dialog closes, rows and cards on the page update their status line without a reload.
- Failures stay visible on the contact afterwards. Nothing is reported only in a toast.

### Routes

| Method and path | Who | Does |
| --- | --- | --- |
| `POST /api/cadence/preview` | everyone signed in | Resolves the selection (ids, or a People filter query string), runs the block rules, calls Cadence's check (14.14), returns will-send and skipped lists and a preview id. No writes. |
| `POST /api/cadence/push` | everyone signed in | Re-runs the block rules, leaves out what the preview's check blocked, creates a batch, claims contacts, sends the first request inline (60-second client timeout, 14.11), hands the rest and any retries to a background thread, returns `{batch_id, results[], pending}`. |
| `GET /api/cadence/batch/{id}` | creator, or admin | Per-contact results so far. |
| `POST /api/cadence/refresh` | everyone signed in | Reads Cadence status by id (14.12) for up to 500 contacts (`crd`, `person_key` pairs): one from Refresh, or the pushed people on a firm page. Writes opt-outs into `contact_suppression`. |
| `POST /api/cadence/suppress` | everyone signed in | Adds a do-not-contact (scope, channel, reason, note). |
| `GET /settings/cadence` | admin | Settings screen, queue counts, recent events, active suppressions. |
| `POST /settings/cadence/test` | admin | Calls Cadence's `ping` with the key from the environment and reports reachability, auth, `sandboxEnabled` and `maxBatch`, never the key. |
| `POST /settings/cadence/lift` | admin | Lifts a suppression. The row is kept for the audit. |
| `POST /settings/cadence/retry` | admin | Moves `failed` pushes back to the queue as a new request with a new `Idempotency-Key`. |

## Shared contract with Bellwether

Section 14 of this plan; its parts are numbered 14.1 to 14.14. Version 1. Cadence serves this API and Bellwether calls it. Bellwether's plan copies this section
word for word. Changes within v1 are additive only (new optional request fields, new response
fields, new warning codes). Anything else is a v2 path.

### 14.1 Base URL and transport

- Bellwether reads Cadence's base URL from env **`CADENCE_API_URL`**, for example
  `https://cadence.pmx.acumen-strategy.com`, with no trailing slash. HTTPS only.
- Requests and responses are `Content-Type: application/json; charset=utf-8`.
- Times are ISO 8601 UTC with `Z` (`2026-11-03T15:04:05Z`). Dates are `YYYY-MM-DD`.
- Cadence ignores request fields it does not know. Bellwether ignores response fields it does not
  know.

### 14.2 Authentication

- Header **`X-Cadence-Api-Key: <key>`** on every request.
- Cadence reads the key from env **`BELLWETHER_API_KEY`**. During a rotation it also accepts env
  **`BELLWETHER_API_KEY_PREVIOUS`**. The comparison is constant-time.
- Bellwether reads the same key from env **`CADENCE_API_KEY`**.
- The key is at least 32 random bytes, base64url, generated by an admin. It is stored only in each
  app's environment (Nomad variables), never in code, docs, chat, logs or a database.
- A missing or wrong key gets 401. If `BELLWETHER_API_KEY` is unset in Cadence, every call gets
  503 `integration_disabled`.

### 14.3 Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/integrations/bellwether/v1/ping` | Key and connectivity check. |
| POST | `/api/integrations/bellwether/v1/contacts` | Push 1 to 500 contacts into the sandbox. |
| POST | `/api/integrations/bellwether/v1/contacts/status` | Status of pushed contacts, by id or by change time. |
| POST | `/api/integrations/bellwether/v1/contacts/check` | The per-contact results a push would give, without writing anything (14.14). |

**Ping**, 200:
```json
{ "ok": true, "version": "1", "sandboxEnabled": true, "maxBatch": 500, "serverTime": "2026-11-03T15:04:05Z" }
```

### 14.4 Push request

Headers: `X-Cadence-Api-Key`, and **`Idempotency-Key: <UUID v4>`** (required).

Envelope:

| Field | Type | Required | Rules |
|---|---|---|---|
| `pushedBy` | object | yes | The Bellwether user who pressed Push. |
| `pushedBy.email` | string | yes | 3 to 254 characters; their sign-in email. Cadence lower-cases it. |
| `pushedBy.name` | string | no | ≤ 200. |
| `pushedBy.microsoftObjectId` | string | no | Entra ID object id (GUID). Matched first. |
| `assignTo` | object or null | no | Owner when it is not the pusher. |
| `assignTo.email` | string | yes, if `assignTo` is given | Matched to a Cadence user. |
| `pod` | string or null | no | A Twenty `podOwner` value such as `ALISA`. Used only when the owner works that pod. |
| `contacts` | array of Contact | yes | 1 to 500 items. |

Contact:

| Field | Type | Required | Rules |
|---|---|---|---|
| `bellwetherContactId` | string | yes | 1 to 200 characters. Stable for the life of the contact in Bellwether. Compared exactly (case-sensitive). Recommended form: `<firmCrd>:<personKey>`. |
| `firstName` | string | yes | 1 to 100 characters after trimming. |
| `lastName` | string | yes | 1 to 100 characters after trimming. |
| `jobTitle` | string or null | no | ≤ 200. |
| `email` | string or null | no | ≤ 254, basic address syntax. |
| `emailVerification` | `"valid"`, `"risky"` or `"unknown"` | no | Default `"unknown"`. `valid`: a mail server confirmed the mailbox. `risky`: catch-all or accept-all. Addresses known to bounce are not sent. |
| `additionalEmails` | string[] | no | ≤ 5. |
| `phone` | string or null | no | ≤ 40. E.164 preferred. Any format accepted; the region is taken from `country`, defaulting to US. |
| `phoneType` | `"direct"`, `"mobile"`, `"office"`, `"main"`, `"toll_free"` or `"unknown"` | no | Default `"unknown"`. |
| `phoneExtension` | string or null | no | Digits, ≤ 10. |
| `additionalPhones` | array of `{ "number": string, "type": phoneType }` | no | ≤ 5. |
| `linkedinUrl` | string or null | no | A person profile (`linkedin.com/in/...`). |
| `city` | string or null | no | ≤ 100. The contact's office city. |
| `state` | string or null | no | ≤ 100. Two-letter US state, or a region name. |
| `country` | string or null | no | ISO 3166-1 alpha-2. Default `"US"`. |
| `individualCrd` | string or null | no | FINRA individual CRD, digits. |
| `designations` | string[] | no | ≤ 20, each ≤ 20, e.g. `"CFP"`. |
| `productInterest` | string[] | no | Twenty `productInterest` values: `PHH`, `TOLLBOOTH`, `ACUBOOTH`, `GLYNAC`. |
| `tags` | string[] | no | ≤ 20, each ≤ 50. Kept in the sandbox only. |
| `notes` | string or null | no | ≤ 5000. Becomes the contact's first sandbox note. |
| `bellwetherUrl` | string or null | no | HTTPS link to the contact in Bellwether. |
| `source` | string or null | no | ≤ 200. Where Bellwether found the person, e.g. `"SEC IAPD"` or `"firm website"`. |
| `collectedAt` | string (date) or null | no | When Bellwether collected the data. |
| `doNotContact` | boolean | no | Default `false`. `true` means the contact is rejected (`do_not_contact`). |
| `firm` | object | yes | The contact's firm. |
| `firm.name` | string | yes | 1 to 200 characters. |
| `firm.crd` | string or null | no | SEC/FINRA firm CRD, digits. |
| `firm.website` | string or null | no | URL. |
| `firm.domain` | string or null | no | Bare domain. Derived from `website` when absent. |
| `firm.city`, `firm.state`, `firm.country` | string or null | no | Main office. Same formats as the contact's. |
| `firm.phone` | string or null | no | Main office line. Kept for reference; never used to match. |
| `firm.aumUsd` | integer or null | no | Regulatory AUM, whole US dollars. |
| `firm.employees` | integer or null | no | |

**Validation.**
- Envelope problems are refused for the whole request with 422. Those are: no `Idempotency-Key`,
  no `pushedBy.email`, `contacts` missing or empty, an `assignTo` with no email.
- More than 500 contacts gets 413.
- A contact with a missing or invalid **required** field is `rejected` / `invalid_field`, with the
  field named in `detail`. The rest of the batch is still processed.
- An invalid **optional** field is dropped with a warning, and the contact is still created. This
  covers a bad email, phone or LinkedIn URL, and an unknown product value.

### 14.5 Normalisation (Cadence applies it; Bellwether should apply the same before sending)

| Value | Rule |
|---|---|
| email | Trim, lower-case the whole address. Dots and `+tags` are kept. |
| phone | Parse to E.164 with the region from `country` (default US). The extension is split off. |
| linkedinUrl | Lower-case. Remove the scheme, `www.` or a country subdomain, the query, the fragment and a trailing slash, giving `linkedin.com/in/<slug>`. Anything that is not `/in/` is dropped. |
| CRDs | Digits only, leading zeros removed. |
| domain | Lower-case. Remove the scheme, `www.` and any path. |

### 14.6 Dedupe rules (in this order; the first hit decides)

1. **Within the request.** A second contact with the same `bellwetherContactId` or the same
   normalised email is `rejected` / `duplicate_in_request`.
2. **Suppression.** The email, a direct or mobile phone, the LinkedIn URL or the
   `bellwetherContactId` is on Cadence's suppression list: `rejected` / `suppressed`.
3. **Internal or blocked.** The email or firm domain is one of Acumen's own domains:
   `rejected` / `internal_contact`. The firm is a blocked account or on the "Never prospects"
   list in Cadence (by domain or normalised name): `rejected` / `blocked_account`.
4. **CRM.** A live Twenty person in Cadence's copy (not deleted, not a team member) has the same
   email (primary or additional), the same LinkedIn URL, or the same phone (primary or
   additional). A pushed phone (`phone`, or an entry of `additionalPhones`, each judged by its own
   type) matches when its type is `direct` or `mobile`, and also when it is `office` or
   `unknown`, unless the number equals `firm.phone` or the main line of the matching person's
   company in Twenty. A phone typed `main` or `toll_free` never matches. Result
   `already_in_crm`, with `matchedOn` and `crmPersonId`. This includes contacts moved from the
   sandbox earlier.
5. **Sandbox.** An ACTIVE sandbox contact has the same `bellwetherContactId`, `individualCrd`,
   email, LinkedIn URL, or `direct`/`mobile` phone. Result `duplicate_in_sandbox`. Empty fields on
   the existing contact are filled from the push, and nothing is ever overwritten. The filled
   fields are listed in `filledFields`. The owner does not change.
6. Otherwise the result is **`created`**. A sandbox contact REMOVED by retention does not block a
   new one.

Outside rule 4, phones typed `office`, `main`, `toll_free` or `unknown`, and `firm.phone`, never
match: they are shared lines. In rule 4, `main`, `toll_free` and `firm.phone` never match. Name
and firm alone never match. When Twenty has a person with the same first and
last name at a company with the same domain or CRD, the contact is still created and carries the
warning `possible_crm_match` with that person's id.

### 14.7 Ownership on push

1. The owner is `assignTo` if given, else `pushedBy`. Cadence matches the user by
   `microsoftObjectId`, then email, then alias, among active users.
2. If that user is an Admin, Junior FO, Senior FO, Sales Leader or Pod Manager, they own the new
   contacts. Otherwise (unknown, Biz Ops, inactive) the contacts are created unclaimed, and
   `owner` is null in the response.
3. The pod is `pod` if the owner works that pod, else the owner's only pod, else null. A null pod
   is chosen in Cadence before Move to CRM.

### 14.8 Push response

HTTP 200 whenever the envelope was valid, even if every contact was rejected:

```json
{
  "pushId": "6f1c2a9e-1b7d-4c1e-9a55-2f0f1d3c8b10",
  "replayed": false,
  "receivedAt": "2026-11-03T15:04:05Z",
  "owner": { "email": "alyssa@acumen-strategy.com", "name": "Alyssa Example" },
  "summary": { "created": 1, "duplicate_in_sandbox": 1, "already_in_crm": 1, "rejected": 1 },
  "results": [
    { "bellwetherContactId": "105943:i:7712001", "result": "created",
      "cadenceContactId": "0b6a...", "reason": null, "detail": null, "matchedOn": null,
      "crmPersonId": null, "crmUrl": null,
      "ownerEmail": "alyssa@acumen-strategy.com", "ownerName": "Alyssa Example",
      "filledFields": [], "warnings": [] },
    { "bellwetherContactId": "105943:i:7712002", "result": "duplicate_in_sandbox",
      "cadenceContactId": "5d21...", "reason": null, "detail": null, "matchedOn": "email",
      "crmPersonId": null, "crmUrl": null,
      "ownerEmail": "leigh@acumen-strategy.com", "ownerName": "Leigh Example",
      "filledFields": ["linkedinUrl"], "warnings": [] },
    { "bellwetherContactId": "288810:n:jane doe", "result": "already_in_crm",
      "cadenceContactId": null, "reason": null, "detail": null, "matchedOn": "linkedin_url",
      "crmPersonId": "8e0f...", "crmUrl": "https://<twenty-host>/object/person/8e0f...",
      "ownerEmail": "andrew@acumen-strategy.com", "ownerName": "Andrew Example",
      "filledFields": [], "warnings": [] },
    { "bellwetherContactId": "288810:n:", "result": "rejected",
      "cadenceContactId": null, "reason": "invalid_field", "detail": "lastName is empty",
      "matchedOn": null, "crmPersonId": null, "crmUrl": null,
      "ownerEmail": null, "ownerName": null, "filledFields": [], "warnings": [] }
  ]
}
```

`results` are in request order, one per contact. Each result:

| Field | Type | Meaning |
|---|---|---|
| `bellwetherContactId` | string | As sent. |
| `result` | `"created"`, `"duplicate_in_sandbox"`, `"already_in_crm"` or `"rejected"` | |
| `cadenceContactId` | string (UUID) or null | The sandbox contact, for `created` and `duplicate_in_sandbox`. |
| `reason` | string or null | `rejected` only: `invalid_field`, `do_not_contact`, `suppressed`, `blocked_account`, `internal_contact`, `duplicate_in_request`. |
| `detail` | string or null | One plain sentence for a person to read. |
| `matchedOn` | `"bellwether_contact_id"`, `"individual_crd"`, `"email"`, `"linkedin_url"`, `"phone"` or null | Set for `duplicate_in_sandbox` and `already_in_crm`. |
| `crmPersonId` | string or null | `already_in_crm`: the Twenty person id. |
| `crmUrl` | string or null | `already_in_crm`: the person in Twenty. |
| `ownerEmail`, `ownerName` | string or null | The sandbox owner (`created`, `duplicate_in_sandbox`), or the Twenty Assigned To (`already_in_crm`). |
| `filledFields` | string[] | `duplicate_in_sandbox`: payload field names written into empty fields. Otherwise `[]`. |
| `warnings` | array of `{ "code": string, "field": string or null, "crmPersonId": string or null, "detail": string }` | Codes: `email_dropped`, `phone_dropped`, `linkedin_dropped`, `product_dropped`, `pod_ignored`, `possible_crm_match`. |

### 14.9 Errors

Body: `{ "error": { "code": string, "message": string, "fields": [ { "path": string, "problem": string } ] } }`
(`fields` is present only for 422).

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_json` | The body is not JSON. |
| 401 | `unauthorized` | The key is missing or wrong. |
| 409 | `idempotency_conflict` | The same `Idempotency-Key` was used with a different body. |
| 409 | `idempotency_in_progress` | The same key is still being processed. Has `Retry-After: 5`. |
| 413 | `payload_too_large` | More than 500 contacts, more than 500 status ids, or a body over 2 MB. |
| 415 | `unsupported_media_type` | Not JSON. |
| 422 | `invalid_request` | Envelope validation (14.4). |
| 429 | `rate_limited` | Over the limit. Has `Retry-After` in seconds. |
| 503 | `integration_disabled` | The key is not configured in Cadence, or the sandbox is switched off. |
| 503 | `unavailable` | Cadence's database or the sandbox is down. Retry later. |
| 500 | `internal` | A bug. Retry once with the same key, then stop and report. |

### 14.10 Idempotency

- Every push request carries an `Idempotency-Key` (UUID v4). One user action gets one key. When
  Bellwether splits a large selection into several requests, each request gets its own key.
- Cadence keeps the key, a SHA-256 of the raw body and the response for **7 days**.
  - Same key and same body: Cadence replays the stored response with `"replayed": true`.
  - Same key and a different body: 409 `idempotency_conflict`.
- After a timeout, a 5xx or a 429, Bellwether retries with the **same** key and body. The backoff
  is 5 s, 30 s, then 2 min, at most 3 retries, and Bellwether honours `Retry-After`.
- Apart from the key, the dedupe rules alone mean a contact pushed again never makes a second
  sandbox contact.

### 14.11 Limits and timing

- 500 contacts per push. 500 ids per status request. 2 MB body.
- 30 push requests and 60 status requests per minute per key. Check requests (14.14) count as push
  requests.
- Cadence answers a 500-contact push within 30 seconds. Bellwether's client timeout is 60
  seconds.

### 14.12 Status endpoint

`POST /api/integrations/bellwether/v1/contacts/status`. The body is exactly one of:
- `{ "bellwetherContactIds": ["105943:i:7712001", "..."] }`, with 1 to 500 ids. The results come
  back in request order, and an id Cadence never received is `not_found`.
- `{ "changedSince": "2026-11-03T00:00:00Z", "cursor": null, "limit": 500 }`. This returns every
  contact ever pushed from Bellwether whose status changed at or after `changedSince`, oldest
  change first, up to `limit` (≤ 500). Follow `nextCursor` until it is null. Bellwether polls this
  way every 10 minutes, and by id when a user opens a contact.

Response 200:
```json
{
  "serverTime": "2026-11-03T15:10:00Z",
  "nextCursor": null,
  "results": [
    { "bellwetherContactId": "105943:i:7712001", "status": "moved_to_crm",
      "cadenceContactId": "0b6a...", "ownerEmail": "alyssa@acumen-strategy.com", "ownerName": "Alyssa Example",
      "campaignName": null, "callCount": 3, "lastActivityAt": "2026-11-10T16:22:00Z",
      "movedToCrmAt": "2026-11-12T14:00:00Z", "crmPersonId": "a91c...",
      "crmUrl": "https://<twenty-host>/object/person/a91c...", "statusChangedAt": "2026-11-12T14:00:00Z" }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `status` | `"in_sandbox"`, `"moved_to_crm"`, `"already_in_crm"`, `"opted_out"`, `"removed"` or `"not_found"` | `in_sandbox`: active in Cadence, claimed or not. `moved_to_crm`: a Cadence user moved it (an undo makes it `in_sandbox` again). `already_in_crm`: the push was refused because the person was already in the CRM. `opted_out`: asked not to be contacted; Bellwether should block further pushes. `removed`: removed in Cadence or by retention; may be pushed again. `not_found`: Cadence never received it. |
| `cadenceContactId` | string or null | |
| `ownerEmail`, `ownerName` | string or null | The sandbox owner. For `moved_to_crm` and `already_in_crm`, the Twenty Assigned To. |
| `campaignName` | string or null | The sandbox campaign the contact is in now. |
| `callCount` | integer | Calls logged in Cadence for this contact. |
| `lastActivityAt` | string or null | |
| `movedToCrmAt` | string or null | |
| `crmPersonId`, `crmUrl` | string or null | Set for `moved_to_crm` and `already_in_crm`. |
| `statusChangedAt` | string | When `status` last changed. |

### 14.13 Field mapping, Bellwether to sandbox

The source column is a suggestion from Bellwether's tables. Bellwether's plan is authoritative for
that column.

| Payload field | Sandbox column | Suggested Bellwether source |
|---|---|---|
| `bellwetherContactId` | `bellwetherContactId` | `<crd>:<person_key>` from `contact_point` / `person` |
| `firstName`, `lastName` | `firstName`, `lastName` | `person.first_name` / `last_name`, or a split of `contact_point.person_name` |
| `jobTitle` | `jobTitle` | `contact_point.title` / `contact.title` |
| `email`, `emailVerification` | `email`, `emailVerification` | `contact_point` kind `email`, `verify_status` |
| `additionalEmails` | `additionalEmails` | other usable email `contact_point`s |
| `phone`, `phoneType`, `phoneExtension` | `phone`, `phoneType`, `phoneExtension` | `contact_point` kind `phone`, `label` |
| `additionalPhones` | `additionalPhones` | other phone `contact_point`s |
| `linkedinUrl` | `linkedinUrl` | `contact_point` kind `linkedin` |
| `city`, `state`, `country` | `city`, `state`, `country` | `person_employment` (current) `city`, `state` |
| `individualCrd` | `individualCrd` | `person.indvl_pk` |
| `designations` | `designations` | `person.designations` |
| `productInterest` | `productInterest` | Bellwether product families mapped to Twenty values |
| `tags` | `tags` | Bellwether list names |
| `notes` | first `SandboxNote` | |
| `bellwetherUrl` | `bellwetherUrl` | |
| `source` | `sourceNote` | `contact_point.source` |
| `collectedAt` | `collectedAt` | `contact_point.found_at` |
| `firm.name` | `companyName` | `firm.business_name`, else `legal_name` |
| `firm.crd` | `companyCrd` | `firm.crd` |
| `firm.website`, `firm.domain` | `companyWebsite`, `companyDomain` | `firm.website` |
| `firm.city`, `firm.state`, `firm.country` | `companyCity`, `companyState`, `companyCountry` | `firm.city`, `state`, `country` |
| `firm.phone` | `companyPhone` | `firm.phone` |
| `firm.aumUsd` | `companyAumUsd` | `firm.raum` |
| `firm.employees` | `companyEmployees` | `firm.total_employees` |
| `pushedBy.email` | `pushedByEmail` | `app_user.email` |
| `pushedBy.microsoftObjectId` | (owner match) | `app_user.ms_oid` |

Sandbox to Twenty is section 8.2.

### 14.14 Check before push

`POST /api/integrations/bellwether/v1/contacts/check`. Headers: `X-Cadence-Api-Key` only. There
is no `Idempotency-Key`; one that is sent is ignored.

- The body is the same as a push (14.4) and is validated the same way: the same 400, 413, 415 and
  422 answers, and the same per-contact `rejected` / `invalid_field` results.
- Cadence applies the same normalisation (14.5), dedupe (14.6) and ownership (14.7) a push would
  apply at that moment, and **writes nothing**: no sandbox contact, no filled field, no note, no
  idempotency record, no push record.
- Response 200 has the push response's shape (14.8), with `"pushId": null` and
  `"replayed": false`. Each result is the one the push would give now. `created` means "would be
  created", and its `cadenceContactId` is null. `filledFields` on a `duplicate_in_sandbox` lists
  the fields a push would fill.
- It counts against the push rate limit (30 requests a minute per key, 14.11). The 500-contact and
  2 MB limits apply.
- Errors are as in 14.9; `idempotency_conflict` and `idempotency_in_progress` never occur.
- Bellwether calls it for the push preview and blocks every contact whose result is
  `already_in_crm` (the email or phone matched a CRM contact; `matchedOn` says which). The push
  itself still returns `already_in_crm` if the CRM changed in between.

## Field mapping

Field names are the contract's (14.4). The source column is Bellwether's: 14.13 leaves it to this
plan.

Rules for every field:

- Never invent a value; send null rather than a placeholder; never send a pattern guess (the
  `usable_contact_point` view already excludes guesses); never send a shared inbox
  (`contact_point.is_role = 1`) as a person's email.
- Normalise as 14.5 says before sending, so Bellwether's own dedupe and Cadence's compare the same
  values: emails trimmed and lower-cased; phones in E.164 with the region from `country` (default
  US) and the extension split off into `phoneExtension`; LinkedIn as `linkedin.com/in/<slug>`, with
  anything that is not `/in/` dropped; CRDs as digits with leading zeros removed; domains
  lower-cased with no scheme, `www.` or path. `bellwetherContactId` is not normalised: Cadence
  compares it exactly, so it goes as stored.
- Keep to 14.4's limits: names, titles, notes and tags are cut to their limit; an email over 254
  characters or a phone over 40 is not sent; lists stop at their maximum count.

### Envelope

| Contract field | Bellwether source | When missing |
| --- | --- | --- |
| `pushedBy.email` | `app_user.email` of the signed-in user, else `app_user.login` when it is an email address | Blocked locally: "Your Bellwether account needs an email address to push" |
| `pushedBy.name` | `app_user.name` | Omitted |
| `pushedBy.microsoftObjectId` | `app_user.ms_oid` (Microsoft sign-ins) | Omitted |
| `assignTo.email` | `app_user.email` of the owner chosen in the dialog, when that is not the pusher | `assignTo` is null, so the pusher owns (14.7) |
| `pod` | Not sent: Bellwether has no pods | null |
| `contacts` | The contacts that passed the block rules, at most 500 per request | Never empty: an empty selection is not sent |

### Contact

| Contract field | Bellwether source | When missing |
| --- | --- | --- |
| `bellwetherContactId` | `people_index.crd \|\| ':' \|\| people_index.person_key`, for example `123456:i:7654321` or `123456:n:jane smith` | Never missing |
| `firstName` | `person.first_name`. For non-SEC rows, the first token of `people_index.name`, using the split in `people_index._extra_people` | Blocked locally: "Needs a first and last name" |
| `lastName` | `person.last_name`. For non-SEC rows, the last token of `people_index.name` | Blocked locally, as above |
| `jobTitle` | `people_index.title` (Schedule A title, else the website or contact title), through `roles.clean_title` | null. The People screen's "Registered representative" filler is not sent. |
| `email` | Best personal email in `usable_contact_point`: `kind='email'`, same `crd` and `person_key`, `is_role=0`; verified first, then confidence, then oldest `id` (the `pi_em` order) | null. Blocked if phone is also missing. |
| `emailVerification` | `contact_point.verify_status` of that email: `valid` → `"valid"`, `catch_all` → `"risky"`, any other usable status → `"unknown"` | Omitted with the email |
| `additionalEmails` | The person's other usable personal emails, in the same order, up to 5. The contract has no status per address. | `[]` |
| `phone` | Best usable phone for the person in the `pi_ph` order (direct, mobile, office, main). Otherwise `firm_current.phone` as the main line. | null. Blocked if email is also missing. |
| `phoneType` | `contact_point.label` when it is `direct`, `mobile`, `office`, `main` or `toll_free`, else `"unknown"`; `"main"` for the firm fallback | Omitted with the phone |
| `phoneExtension` | The extension split off the stored number, digits only | null |
| `additionalPhones` | Remaining usable phones for the person, as `{number, type}`, up to 5 | `[]` |
| `linkedinUrl` | `usable_contact_point` `kind='linkedin'` with `verify_status = 'matched'`. A `probable` profile goes in `notes` instead (see below). | null |
| `city`, `state` | `people_index.bcity`, `people_index.bstate` (SEC branch) | null |
| `country` | `firm_current.country` as ISO 3166-1 alpha-2 (`United States` → `US`) | null (Cadence then assumes US) |
| `individualCrd` | `person.indvl_pk` (`people_index.indvl_pk`) | null (Schedule A and website people) |
| `designations` | `person.designations` (CFP, CFA and so on), up to 20 | `[]` |
| `productInterest` | The product in view (People `on`, firm page `p`) or the dialog choice, mapped as Product interest says. Never `TOLLBOOTH` (decision 25). | `[]` |
| `tags` | Bellwether facts the contract has no field for (below) | `[]` |
| `notes` | The dialog note (up to 500 characters), then Bellwether's lines (below). The contract allows 5000, so the dialog's 500 stays. | null when both are empty |
| `bellwetherUrl` | `app.public_url` + `/firm/{crd}#p-<anchor>`, with the anchor built as `_person_card` builds it, else the request's base URL | null when the address is not HTTPS (a local run) |
| `source` | One phrase, up to 200 characters: where the person came from, then where the email came from (below) | Never missing |
| `collectedAt` | `contact_point.found_at` of the email sent, else of the phone sent, as a date | null |
| `doNotContact` | Always `false`. A contact with any active suppression is not sent at all (see Compliance and Contract gap 15). | Not applicable |
| `firm.name` | `firm_current.business_name`, else `legal_name`, cased the way the firm page shows it (`nice_name`) | Never missing (the index joins `firm_current`) |
| `firm.crd` | `people_index.crd` | Never missing |
| `firm.website` | `firm_current.website` | null |
| `firm.domain` | Derived from `firm_current.website` | null |
| `firm.city`, `firm.state`, `firm.country` | `firm_current.city`, `firm_current.state`, `firm_current.country` (alpha-2) | null |
| `firm.phone` | `firm_current.phone` | null |
| `firm.aumUsd` | `firm_current.raum` | null |
| `firm.employees` | `firm_current.total_employees` | null |

### Product interest

| Bellwether product (`config/products.yml`) | `productInterest` |
| --- | --- |
| PHH Fund I, PHH 1031, PHH JV (family PHH) | `["PHH"]` |
| AcuBooth | `["ACUBOOTH"]` |
| Glynac | `["GLYNAC"]` |
| ATS (Acumen Talent Solutions) | `[]`, and no ATS tag of any kind: an ATS-list contact goes exactly as if no product were chosen (decision 25) |
| None chosen | `[]` |

Bellwether never sends `TOLLBOOTH` and never sends the tag `list:ATS` (decision 25). The payload
builder enforces both whatever the caller passes, and a test proves it.

### What the contract has no field for

Each of these is folded into `tags`, `source` or `notes`. Nothing is left over, so none of them is a
contract gap.

| Bellwether fact | Sent as | How |
| --- | --- | --- |
| The exact product list | `tags` | `list:<product name>`, for example `list:PHH 1031`. Never `list:ATS`: an ATS push carries no list tag (decision 25). |
| ICP designations | `tags` | `designation:<label>` for each of the person's designations on the pushed product (`people_index.desig` through `designations.label_of`); every product's except ATS's when none is chosen, so ATS never shows through a designation tag |
| Firm type | `tags` | `firm type:<label>` from `firm_class.category` |
| Disclosure on SEC record | `tags` | `sec disclosure` when `people_index.has_disclosure` |
| LinkedIn matched or probable | `linkedinUrl` or `notes` | A `matched` profile is `linkedinUrl`. A `probable` one is not, because Cadence dedupes on that field (14.6 rules 4 and 5) and a wrong profile would match someone else. It goes in `notes` as "Possible LinkedIn, not confirmed: <url>". |
| Where the person and the email came from | `source` | The person from `people_index.src`: `SEC IAPD` (`sec`), `Form ADV Schedule A` (`adv`) or `Firm website or web search` (`web`). Then the email: a fixed phrase for each `contact_point.source`, plus "mailbox confirmed" when `verify_status = 'valid'`. Example: `SEC IAPD; email from the firm website, mailbox confirmed`. |
| Firm legal name | `notes` | "Legal name: <legal_name>" when it differs from `firm.name` |
| SEC record link | `notes` | "SEC record: <person.iapd_link>" |
| Full name | Not sent | Cadence builds the name from `firstName` and `lastName` |

Tags stay within 20, each within 50 characters, in the order above. Bellwether's lines in `notes`
follow the user's note after a blank line and are cut first if the total would pass 5000.

### Contract gaps

The contract covers most of what this plan asked of it. Those items are struck through, with the
part of section 14 that settles each. Two remain open, each with the default v1 builds. The
contract text above is not changed.

1. ~~**Bellwether contact id as a dedupe key.** Cadence must return the same Cadence id when the
   same Bellwether id arrives twice. Bellwether's retries depend on it.~~ Covered: 14.6 rule 5
   matches on `bellwetherContactId`, and 14.10 replays a retried request.
2. ~~**Phone type.** Cadence's `PersonCache` has `phone` and `additionalPhone` and no label.
   Without one, reps cannot tell a firm switchboard from a direct line.~~ Covered: `phoneType` and
   `additionalPhones[].type` (14.4); shared lines never match (14.6).
3. ~~**Email status and source.** Needed so a published-but-unconfirmed address is not treated as
   verified.~~ Covered: `emailVerification` and `source` (14.4).
4. ~~**Firm identity.** The SEC firm CRD, so Move to CRM can find or create the right Twenty
   company. `CompanyCache` has no CRD field.~~ Covered: `firm.crd` (14.4).
5. ~~**Owner by email.** The contract must say what happens when the pusher or chosen owner has no
   Cadence user: refused, unassigned, or a default owner.~~ Covered: 14.7, an unknown owner means
   the contact is created unclaimed.
6. ~~**Product interest and note.** Whether Cadence accepts them.~~ Covered: `productInterest` and
   `notes` (14.4).
7. ~~**Status by Bellwether id, in bulk.** One call for many contacts, returning sandbox, moved to
   CRM (with the Twenty id and time), already in CRM, removed, opted out or do not call, and
   owner.~~ Covered: 14.12, by id or by `changedSince`.
8. ~~**Opt-outs.** Cadence must refuse, or flag, a pushed contact it already holds as opted
   out.~~ Covered: 14.6 rule 2 (`rejected` / `suppressed`) and 14.12 (`opted_out`). The second
   half, honouring separate do-not-email and do-not-call flags from Bellwether, is gap 15.
9. ~~**Idempotency.** The same key with the same body returns the original response, for how
   long, and what a key reused with a different body returns.~~ Covered: 14.10.
10. ~~**Rate limits.** `429` with `Retry-After`, and the batch limit.~~ Covered: 14.9 and 14.11.
11. ~~**Key rotation.** Two keys valid at once while one is replaced.~~ Covered: 14.2.
12. ~~**Re-push and updates.** Whether a second push of a known contact updates its details or is
    only reported as a duplicate.~~ Covered: 14.6 rule 5, empty fields are filled and nothing is
    overwritten.
13. ~~**Removal.** Whether a sandbox contact deleted for retention or erasure is reported, so
    Bellwether can stop showing "In Cadence".~~ Covered: 14.12 `removed`. Erasure is not
    signalled apart: gap 16.
14. ~~**Field limits.** Maximum lengths for name, title, note and URLs, so Bellwether trims before
    sending rather than being refused.~~ Covered: 14.4.

Still open:

15. **Separate do-not-email and do-not-call.** The contract has only `doNotContact`, and `true`
    rejects the contact (14.4). A Bellwether suppression can cover one channel.
    **v1 default:** a Bellwether email-only or call-only suppression blocks the push locally, the
    same as an all-channel one. Asked of Cadence: optional `doNotEmail` and `doNotCall` request
    fields, which v1 allows as additive.
16. **An erasure signal.** The status endpoint has `opted_out` and `removed` but nothing for
    erasure, so Bellwether cannot tell an erasure from a retention removal.
    **v1 default:** no erasure signal. `opted_out` blocks all channels in Bellwether. A `removed`
    contact may be pushed again by anyone (14.12 says it may; decision 16 gives admins nothing
    extra). Cadence's own suppression list still refuses an erased person (14.6 rule 2). Asked of
    Cadence: an optional response field such as `erasedAt`, which v1 allows as additive.

Two contract changes arrived with Rahul's decisions, and this plan follows both:

- **14.14 Check before push** gives the preview the push's own per-contact answers without writing
  anything. Bellwether uses it to block `already_in_crm` contacts before sending (decision 17).
- **14.6 rule 4 now matches more phones against the CRM**: `office` and `unknown` as well as
  `direct` and `mobile`, unless the number is `firm.phone` or the matched person's company main
  line. Bellwether always sends `firm.phone`, and the main-line fallback goes as `phoneType`
  `main`, so a switchboard never matches a CRM person.

## Data model and migration

All in `prospect/cadence.py` `SCHEMA`, in the codebase's style (TEXT ISO timestamps,
`INTEGER PRIMARY KEY`), created at startup by `_init_optional(c, "cadence")`.

```sql
-- One row per contact ever pushed: where it stands now.
CREATE TABLE IF NOT EXISTS cadence_push (
    id              INTEGER PRIMARY KEY,
    crd             TEXT NOT NULL,
    person_key      TEXT NOT NULL,           -- i:<indvl_pk> | n:<name key>, as in contact_point
    indvl_pk        TEXT,                    -- SEC individual CRD when registered
    name            TEXT NOT NULL,           -- as sent
    email           TEXT,                    -- as sent, normalised
    phone           TEXT,
    linkedin        TEXT,
    state           TEXT NOT NULL,           -- sending | retry_wait | in_sandbox | in_crm
                                             -- | moved_to_crm | opted_out | rejected | removed
                                             -- | failed
    reason          TEXT,                    -- Cadence's reason and detail, or the last error, in words
    cadence_id      TEXT,                    -- cadenceContactId
    crm_id          TEXT,                    -- crmPersonId, once in the CRM
    crm_url         TEXT,                    -- crmUrl
    cadence_owner   TEXT,                    -- ownerEmail as Cadence reports it; NULL = unclaimed
    cadence_owner_name TEXT,                 -- ownerName
    owner_email     TEXT,                    -- assignTo.email Bellwether sent; NULL = the pusher
    product         TEXT,                    -- Bellwether product key, e.g. phh_1031
    pushed_by       TEXT NOT NULL,           -- app_user.login of the latest push
    first_pushed_at TEXT NOT NULL,
    pushed_at       TEXT,                    -- last time Cadence accepted it
    moved_at        TEXT,                    -- movedToCrmAt
    batch_id        TEXT,
    request_key     TEXT,                    -- Idempotency-Key of the request that carries it
    payload_hash    TEXT,                    -- sha256 of the contact as sent
    claimed_at      TEXT,                    -- set while state = 'sending'
    status_at       TEXT,                    -- last read of Cadence's status
    status_changed_at TEXT,                  -- Cadence's statusChangedAt
    UNIQUE (crd, person_key)
);
CREATE INDEX IF NOT EXISTS ix_cadence_push_state ON cadence_push (state);
CREATE INDEX IF NOT EXISTS ix_cadence_push_req ON cadence_push (request_key);
CREATE INDEX IF NOT EXISTS ix_cadence_push_indvl ON cadence_push (indvl_pk) WHERE indvl_pk IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cadence_push_email ON cadence_push (email) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cadence_push_li ON cadence_push (linkedin) WHERE linkedin IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_cadence_push_cid ON cadence_push (cadence_id);  -- not unique: two
-- Bellwether rows (a rep's SEC row and a website spelling) can resolve to one Cadence contact.

-- One row per push request (a chunk of a batch). The body is kept exactly as sent, so a retry
-- resends the same bytes under the same Idempotency-Key (14.10). It is cleared once the request ends.
CREATE TABLE IF NOT EXISTS cadence_request (
    request_key   TEXT PRIMARY KEY,          -- Idempotency-Key: a fresh UUID v4 per request
    batch_id      TEXT NOT NULL,
    body          TEXT,                      -- JSON exactly as sent; NULL once finished
    body_sha256   TEXT NOT NULL,
    contacts      INTEGER NOT NULL,          -- 1 to 500
    state         TEXT NOT NULL,             -- sending | retry_wait | done | failed
    retries       INTEGER NOT NULL DEFAULT 0, -- of the contract's 3
    first_sent_at TEXT NOT NULL,             -- the key is retried for 7 days from this, no longer
    next_retry_at TEXT,
    claimed_at    TEXT,                      -- set while state = 'sending'
    last_error    TEXT                       -- http_<code> <error.code> | timeout | connection
);
CREATE INDEX IF NOT EXISTS ix_cadence_request_due ON cadence_request (state, next_retry_at);

CREATE TABLE IF NOT EXISTS cadence_batch (
    id          TEXT PRIMARY KEY,            -- uuid4
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    origin      TEXT NOT NULL,               -- people | firm
    filter_qs   TEXT,                        -- the People filter, for "all matching"
    product     TEXT,
    owner_email TEXT,
    note        TEXT,
    requested   INTEGER NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    finished_at TEXT
);

-- Append only: the audit. Never the API key, never contact values.
CREATE TABLE IF NOT EXISTS cadence_push_event (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,               -- login, or 'system' for the job
    crd         TEXT NOT NULL,
    person_key  TEXT NOT NULL,
    batch_id    TEXT,
    action      TEXT NOT NULL,               -- push | retry | skipped | result | status
                                             -- | suppress | lift | hold
    outcome     TEXT,                        -- created | duplicate_in_sandbox | already_in_crm
                                             -- | rejected:<reason> | replayed | http_<code>
                                             -- | timeout | skipped:<rule> ...
    http_status INTEGER,
    request_key TEXT,
    detail      TEXT                         -- JSON: reason, warnings, filledFields, fields sent
                                             -- (names only), Cadence ids
);
CREATE INDEX IF NOT EXISTS ix_cpe_person ON cadence_push_event (crd, person_key, at);
CREATE INDEX IF NOT EXISTS ix_cpe_at ON cadence_push_event (at);

-- Do not contact. A lifted row is kept, for the audit.
CREATE TABLE IF NOT EXISTS contact_suppression (
    id        INTEGER PRIMARY KEY,
    scope     TEXT NOT NULL,                 -- person | email | phone | domain | firm
    value     TEXT NOT NULL,                 -- crd:person_key | norm_email | norm_phone | domain | crd
    channel   TEXT NOT NULL DEFAULT 'all',   -- all | email | call
    reason    TEXT NOT NULL,                 -- opted_out | do_not_call | do_not_email | erasure | other
    source    TEXT NOT NULL,                 -- cadence | manual
    note      TEXT,
    set_by    TEXT,
    set_at    TEXT NOT NULL,
    lifted_by TEXT,
    lifted_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_suppression_active
    ON contact_suppression (scope, value, channel) WHERE lifted_at IS NULL;

CREATE TABLE IF NOT EXISTS cadence_state (
    k TEXT PRIMARY KEY,                      -- backoff_until | hold | status_since | push_slot
                                             -- | status_slot | last_sync_ok | last_auth_error
    v TEXT
);
```

Migration: these tables only. No existing table changes, no backfill. To roll back, drop the six
tables. Like every other table, they are created by `CREATE TABLE IF NOT EXISTS` at startup, so a
deploy creates them; nothing is run by hand.

### Push states

| State | Shown as | Next |
| --- | --- | --- |
| `sending` | Sending | A push result; `retry_wait` on a retryable error; `retry_wait` if the claim is older than 5 minutes |
| `retry_wait` | Waiting to retry | Resent with the same key and body when `next_retry_at` passes, or held while Cadence refuses the key or is switched off. `failed` after the third retry, or once the key is 7 days old. |
| `in_sandbox` | In Cadence · owner, or unclaimed | Status sync: `moved_to_crm`, `in_crm`, `opted_out` or `removed` |
| `in_crm` | Already in the CRM · owner | Terminal |
| `moved_to_crm` | Moved to CRM · date | Status sync carries on: an undo in Cadence makes it `in_sandbox` again |
| `opted_out` | Opted out (Cadence) | Terminal. An all-channel suppression with `source='cadence'` blocks every push. |
| `rejected` | Cadence refused: reason | Anyone can push again, as a new request with a new key |
| `removed` | Removed from Cadence | Anyone can push again, as a new request with a new key (decision 16) |
| `failed` | Push failed: reason | Anyone can push again, or an admin uses Retry, as a new request with a new key |

### Sending, retries and errors

Every call follows 14.2, 14.9, 14.10 and 14.11.

- **Headers.** `X-Cadence-Api-Key` with the key from `CADENCE_API_KEY`, `Content-Type:
  application/json; charset=utf-8`, and on pushes an `Idempotency-Key`. The client timeout is 60
  seconds.
- **Chunking.** A batch is split into requests of at most 500 contacts (the contract's limit, or
  the `maxBatch` that `ping` reports if lower). Bellwether's own cap, `cadence.max_per_push` (200),
  stays, so a push is one request unless an admin raises the cap.
- **Idempotency-Key.** A fresh UUID v4 for each request, generated when the request is built and
  stored as `cadence_request.request_key` and on each of its contacts as `cadence_push.request_key`.
  The exact body is stored with it. The first request is sent inside the web request; the rest, and
  every retry, go to a background thread in the web process, the same way `prospect/api_view.py`
  hands off slow work.
- **Retries (14.10).** After a timeout, a connection error, a `429`, a `503 unavailable` or any
  other 5xx except `500`, the request is resent with the same key and the stored body, byte for
  byte: after 5 s, 30 s, then 2 min, at most 3 retries. A `Retry-After` longer than the step wins
  and is written to `cadence_state.backoff_until`, so no sender calls Cadence before it. A key is
  retried only within 7 days of `first_sent_at`, while Cadence still holds it. After the third
  retry, or past 7 days, the request and its contacts are `failed`. A person then pushes again, or
  an admin uses Retry, which builds a new request with a new key; the contract's dedupe (14.6)
  means that never makes a second sandbox contact.
- **Who resends.** Whoever moves a due request from `retry_wait` to `sending` (one `UPDATE ...
  WHERE state = 'retry_wait' AND next_retry_at <= :now RETURNING`) sends it: normally the background
  thread, and the `cadence_sync` job if the web process died. A request claimed more than 5 minutes
  ago (longer than one attempt) goes back to `retry_wait` with its key and body unchanged.
- **Error answers (14.9).** The `error.code` in the body decides between the two `409`s and the two
  `503`s.

  | Answer | Bellwether does |
  | --- | --- |
  | `200`, `replayed` true or false | Reads the results per contact (below). A replay is read like the original. |
  | Timeout, connection error, `429 rate_limited`, `503 unavailable`, another 5xx | Retries as above. |
  | `500 internal` | Retries once with the same key and body, then `failed` plus an alert. |
  | `409 idempotency_in_progress` | Waits `Retry-After` (5 s) and resends the same key and body. These waits do not use up the 3 retries; after 60 seconds of them the request counts as timed out. |
  | `401 unauthorized` | Holds the whole queue without using retries: `cadence_state.hold`, an alert "Cadence refused Bellwether's key". The contract has no 403. |
  | `503 integration_disabled` | Holds the whole queue the same way, with the alert "Cadence has the Bellwether integration switched off". |
  | `409 idempotency_conflict`, `400 invalid_json`, `413 payload_too_large`, `415 unsupported_media_type`, `422 invalid_request` | `failed` plus an alert. These mean a bug or contract drift, and resending cannot fix them. |
  | Any other answer | `failed` plus an alert. |

  While held, nothing is sent. Each `cadence_sync` run calls `ping` and lifts the hold when it
  answers `200` with `sandboxEnabled: true`. A held request still fails once its key is 7 days old.
- **Push results (14.8).** One per contact, in request order:

  | `result` | State | Shown as |
  | --- | --- | --- |
  | `created` | `in_sandbox` | Added to sandbox (owner: X), or unclaimed when `ownerEmail` is null |
  | `duplicate_in_sandbox` | `in_sandbox` | Already in sandbox (owner: X). `filledFields` goes in the event. |
  | `already_in_crm` | `in_crm` | Already in the CRM (owner: X) · matched on <field>, with `crmPersonId` and `crmUrl` stored. This is the backstop for a CRM that changed after the preview's check. |
  | `rejected` | `rejected`, with `reason` and `detail` | Refused: the reason in words ("on Cadence's do-not-contact list", "Cadence blocks this firm", "an Acumen address", "a required field is missing: <detail>") |

  A `rejected` / `suppressed` result also writes a `contact_suppression` row (person scope, channel
  `all`, source `cadence`), so nobody offers that person again. `warnings` are stored in the event
  and shown after the result line.
- **Secrets**: the API key is never logged, put in job output, written to `detail` or included in
  an exception message. The client builds its own error strings and does not echo request headers.

### Reading Cadence's status (14.12)

- **Every 10 minutes** the `cadence_sync` job asks for `changedSince` = `cadence_state.status_since`,
  `cursor` null, `limit` 500, and follows `nextCursor` until it is null. When the run completes, it
  stores the `serverTime` of the run's first page as the next `status_since`. Changes made while it
  paged are read again next time, and applying one twice changes nothing. The first run starts from
  the earliest `first_pushed_at`. A run that fails part-way keeps the old `status_since`.
- **By id** when a user opens the People & contacts tab or presses Refresh (see UI), up to 500 ids
  per request. A contact read in the last minute is not asked again. A by-id read applies each
  status exactly as the sync does, opt-outs included (decision 23).
- Each status sets the state, `cadence_owner` and `cadence_owner_name` (for `moved_to_crm` and
  `already_in_crm`, Cadence reports the Twenty Assigned To), and `status_changed_at`:

  | `status` | State | Also |
  | --- | --- | --- |
  | `in_sandbox` | `in_sandbox` | A null owner shows as unclaimed. |
  | `moved_to_crm` | `moved_to_crm` | `crm_id`, `crm_url`, `moved_at` |
  | `already_in_crm` | `in_crm` | `crm_id`, `crm_url` |
  | `opted_out` | `opted_out` | A `contact_suppression` on all channels with `source='cadence'`, for the person and for the email that was sent. People and the person card then show "Opted out (Cadence)" (decision 23). |
  | `removed` | `removed` | |
  | `not_found` | Unchanged while `sending` or `retry_wait`; otherwise `failed`, "Cadence has no record of it" | By-id reads only |

### Rate limiting (Bellwether's own)

- `cadence.max_per_push`, default 200 contacts per push, under the contract's 500 per request.
- `cadence.daily_per_user`, default 500 contacts per user per rolling 24 hours, counted from
  `cadence_push_event` `push` actions.
- Outgoing pace, under 14.11's 30 push and 60 status requests per minute per key: at most one push
  request every 2.5 seconds and one status request every 1.25 seconds, across every process. A
  check request (14.14) counts as a push request and takes a push slot. Each sender reserves its
  slot with one `UPDATE ... RETURNING` on `cadence_state` (`push_slot` or `status_slot`: the later
  of now and the last slot plus the gap) and waits for it.
- Nothing is sent before `backoff_until`, or while the queue is held.
- Status: one `changedSince` run every 10 minutes, plus by-id reads when a user opens a contact or
  presses Refresh.

### Audit

- Every skip, push, retry, result, status change, suppression, lift and hold is a
  `cadence_push_event` row with actor and time. The preview writes nothing, so its skips are
  recorded when the push runs the same rules again.
- Settings › Cadence lists the latest 100 events, with filters by user and outcome.
- The person card tooltip shows that contact's history.
- Diagnostics print counts by state and outcome for the last day, with no contact values.

### The `cadence_sync` job

`Job("cadence_sync", "Cadence", ..., "scripts.cadence_sync", every_hours=1/6, timeout_s=540,
lane="cadence", group="contacts", min_gap_minutes=5)`, every 10 minutes. It gets the new `cadence`
lane in `jobs.LANES`, so the 10-minute pace does not wait behind the slow AI and search jobs in the
`search` lane. Its `backlog_sql` counts `cadence_request` rows in `retry_wait` due now plus stale
`sending` rows. It does nothing unless `cadence.enabled` is on and `CADENCE_API_URL` and
`CADENCE_API_KEY` are set. Each run: check a hold with `ping`; unstick stale claims; fail requests
whose key is 7 days old; resend due requests a dead process left; read status by `changedSince`;
and write any opt-outs it learns into `contact_suppression`.

## Dedupe from Bellwether's side

Order of checks for each selected contact, all in the preview and repeated in the push:

1. **Same id within one selection**: collapsed to one.
2. **Same contact pushed before, or right now by someone else.** `UNIQUE (crd, person_key)`, claimed
   atomically:

   ```sql
   INSERT INTO cadence_push (crd, person_key, ..., state, claimed_at, pushed_by, first_pushed_at)
   VALUES (..., 'sending', :now, :login, :now)
   ON CONFLICT (crd, person_key) DO UPDATE
      SET state = 'sending', claimed_at = excluded.claimed_at, pushed_by = excluded.pushed_by,
          reason = NULL
    WHERE cadence_push.state IN ('rejected', 'failed', 'removed')
   RETURNING id;
   ```

   No row back means someone holds it. If two users push the same person at the same moment,
   Postgres lets one insert win and the other gets no row. The loser sees "Already pushed by Ana
   Ruiz on 2 Nov" or "Being pushed by Ana Ruiz now".
3. **Same person under another firm.** A matching `indvl_pk` in `cadence_push` with a different
   `crd` (a rep who moved firms or is registered at two): skipped as "Already pushed from <firm>".
4. **Same email or LinkedIn under another key.** This happens when a website spells a name
   differently from the SEC row (`n:` against `i:`). It is skipped as "Same email as <name> at
   <firm>, pushed <date>". Within one selection, the first contact with an email wins, so Cadence
   never sees `duplicate_in_request`.
5. **The CRM check** (14.14, decision 17), in the preview: Cadence answers what a push would give
   now, without writing anything. `already_in_crm` (the email, phone or LinkedIn URL matched a CRM
   person; `matchedOn` says which) and `rejected` are skipped and not sent.
6. **Cadence's own dedupe** (14.6) on the push itself, against its suppression list, the CRM and
   the sandbox, by `bellwetherContactId`, `individualCrd`, email, LinkedIn URL and phone. The
   answer shows per contact. A `duplicate_in_sandbox` answer is recorded as `in_sandbox`, not as an
   error; Cadence fills that contact's empty fields from the push and keeps its owner. An
   `already_in_crm` answer here means the CRM changed after the preview, and is recorded as
   `in_crm`.

Re-push rules: `rejected`, `failed` and `removed` can be pushed again by anyone (decision 16), and
the claim above allows it. `opted_out` and every other state cannot be pushed again. Every re-push
is a new request with a new `Idempotency-Key`.

## Compliance

Bellwether has no opt-out data today. This build adds:

- **`contact_suppression`**, with scopes person, email, phone, email domain and firm, and channels
  all, email and call.
  - In v1 every active match **blocks** the push, whatever its channel. The contract has only
    `doNotContact`, which rejects (Contract gap 15), so a Bellwether email-only or call-only
    suppression blocks the push locally. The channel is kept for when the contract can carry it.
  - Suppressed people show "Do not contact" on People and the person card, and can be filtered.
- **Cadence opt-outs come back** (decision 23). A status of `opted_out`, read by the
  `cadence_sync` job every 10 minutes (`changedSince`) or by a by-id refresh, or a push answered
  `rejected` / `suppressed`, writes a `contact_suppression` on all channels with
  `source='cadence'`, so nobody pushes that person again from Bellwether. The person card and the
  People list show "Opted out (Cadence)". Cadence also checks its own suppression list on every
  push (14.6 rule 2).
- **Firm status.** A firm marked `customer` in `firm_status` blocks the push. `disqualified` warns
  and needs a tick. The values come from `STATUSES` in `prospect/webapp.py`.
- **Already in the CRM.** The preview's check (14.14) blocks every contact whose email, phone or
  LinkedIn URL matches a CRM person (decision 17), and the push's own rule 4 catches any that
  appear in between.
- **Nothing unconfirmed is sent.** Only `usable_contact_point` values are sent: no pattern guesses,
  no addresses that bounce or have no mail server, and no shared inboxes as a person's email.
- **Provenance travels.** `emailVerification`, `source`, `phoneType` and `collectedAt` go with every
  contact, and a LinkedIn profile goes as `linkedinUrl` only when it is matched, so Cadence can
  answer "where did you get my details".
- **Erasure.** The contract has no erasure signal (Contract gap 16), so v1 has none. `opted_out`
  blocks all channels in Bellwether, and Cadence's suppression list refuses an erased person if a
  `removed` contact is pushed again. Bellwether does not hide anyone's `contact_point` emails and
  phones on its own (Decisions, 9).
  The SEC roster row is a public record and stays.
- **Cadence's plan owns the rest:** recording consent, retention of contacts that never convert, and
  the GDPR basis for the sandbox.

## Credentials, config and deploy

Names only. Values are never written in chat, code, git, logs, job output or diagnostics.

| What | Setting key | Env var | Where the value lives |
| --- | --- | --- | --- |
| Cadence base URL | none: environment only | `CADENCE_API_URL` (14.1) | The Nomad variable `nomad/jobs/bellwether` key `cadence_url`; locally `.env` |
| Bellwether→Cadence API key | none: environment only, never stored in `app_setting` (14.2) | `CADENCE_API_KEY` (14.2) | The Nomad variable key `cadence_key`; locally `.env`. Cadence holds the same value as `BELLWETHER_API_KEY`. |
| Push switched on | `cadence.enabled` (off by default) | none | Settings › Cadence |
| Caps | `cadence.max_per_push`, `cadence.daily_per_user` | none | Settings › Cadence |

There is no setting for who may push: everyone can (decision 16).

Settings › Cadence shows whether the URL and key are set, never their values, and has no field to
type them in.

Key rotation (14.2): an admin generates a new key; Cadence sets it as `BELLWETHER_API_KEY` and keeps
the old one as `BELLWETHER_API_KEY_PREVIOUS`; Bellwether's `cadence_key` is changed and the job
restarted; then Cadence drops the previous key.

Deploy changes:

- **`nomad/bellwether.nomad.hcl`**: add two guarded lines to the existing `nomadVar` template:
  `{{ with .cadence_url }}CADENCE_API_URL={{ . }}{{ end }}` and
  `{{ with .cadence_key }}CADENCE_API_KEY={{ . }}{{ end }}`. The guard matters, because a missing
  variable would otherwise render a non-empty placeholder that Bellwether would take as configured.
- **Network**: the container on `worker-5` must reach Cadence's HTTPS address. Use Cadence's public
  hostname through the gateway unless both teams agree on an internal address. Settings › Cadence ›
  Test connection proves it from the running allocation.
- **No changes needed** to the image (`requests` is already used in `prospect/net.py`), the CSP (the
  call is server-side), CI or `release_workspace`. The Dockerfile's `qa_requirements` gate picks up
  the new offline tests by itself.
- **Docs**: `.env.example` gets `CADENCE_API_URL` and `CADENCE_API_KEY` with empty values, commented
  out. `DEPLOY.md` adds them to the environment list. `README.md` adds `prospect/cadence.py` to
  "Files worth knowing".
- **Rollout**: deploy with `cadence.enabled` off, set the URL and key, run Test connection, push one
  test contact, then switch it on for everyone.

## Build order, tests, risks

### Build order (one pass)

Steps 4, 5 and 7 implement the contract, as copied in Shared contract with Bellwether.

1. `prospect/cadence.py` schema; register `"cadence"` in `_init_optional`.
2. Settings Specs and the Settings › Cadence screen, with Test connection, queue counts, events and
   suppressions.
3. The contact record builder (`people_index` + `usable_contact_point` + `person` + `firm_current`
   + `firm_class` + `contact_suppression` + `firm_status`), the block rules, and the CRM check
   (14.14) in the preview.
4. The payload mapping with the 14.5 normalisation and the 14.4 limits, and response parsing, in one
   function each.
5. Claim and dedupe; batch, chunk and stored-body handling; the HTTP client with the
   `X-Cadence-Api-Key` header, the 60-second timeout, a UUID v4 `Idempotency-Key` per request, the
   14.10 retries, pacing and redaction.
6. `prospect/cadence_view.py` routes; register the router.
7. `scripts/cadence_sync.py`, the `cadence` lane and the `cadence_sync` job.
8. People list: selection, bar, dialog, results, status line, filter, export columns.
9. Person card: button, status, select, the by-id read, Refresh, Do not contact.
10. `health.alerts()` entries and diagnostics lines.
11. Docs and the jobspec lines.
12. Tests (below). Then the live acceptance.

### Tests

- **`scripts/fake_cadence.py`** (new): an offline fake Cadence, a small `http.server` on
  localhost built from 14.3 to 14.12, used by the tests below. It:
  - checks the `X-Cadence-Api-Key` header, a UUID v4 `Idempotency-Key`, the envelope rules, the
    500-contact and 2 MB limits and the content type;
  - keeps each key with a SHA-256 of the raw body, replays the stored response with
    `"replayed": true`, and answers `409 idempotency_conflict` to the same key with another body;
  - can be told to answer each result type (`created`, `duplicate_in_sandbox` with `filledFields`,
    `already_in_crm`, `rejected` with each reason) and each warning code;
  - serves the check endpoint (14.14) from the same rules with nothing written: `pushId` null,
    `created` with a null `cadenceContactId`, and `already_in_crm` when a seeded CRM person shares
    the email or the phone (office and unknown phones too, unless the number is `firm.phone`);
  - can be told to answer each error: `400 invalid_json`, `401`, `409 idempotency_in_progress` with
    `Retry-After: 5`, `413`, `415`, `422` with `fields`, `429` with `Retry-After`, `500 internal`,
    `503 integration_disabled`, `503 unavailable`, a timeout and a dropped connection;
  - serves the status endpoint by id (with `not_found`) and by `changedSince` over several pages
    with `nextCursor`;
  - counts requests per minute by kind.
- **`scripts/qa_requirements.py`** (offline; runs in the Docker build), against the fake:
  - every mapped field, including each "when missing" rule, the 14.5 normalisation, the 14.4
    limits, the `emailVerification` and product mappings and the folds into `tags`, `source` and
    `notes`;
  - `list:ATS` and `TOLLBOOTH` are never sent, from any entry point, including an ATS-list push
    (no product and no ATS tag) and a caller that asks for them (decision 25);
  - the preview's check: `already_in_crm` by email and by phone blocks the contact, shown as
    "Already in the CRM" with the matched field, and it is not sent; a check that times out lets
    the push go ahead with a note; `401` disables the push; a push without a fresh preview checks
    first; an `already_in_crm` push result is still recorded as `in_crm`;
  - guesses and shared inboxes are never sent, the main-line fallback has `phoneType` `main`, and a
    probable LinkedIn goes only in `notes`;
  - each push result maps to its state, and `rejected` / `suppressed` writes a suppression;
  - each error code does what Sending says: `401` and `503 integration_disabled` hold without using
    retries and `ping` lifts the hold; `409 idempotency_in_progress` waits and resends; `409
    idempotency_conflict`, `400`, `413`, `415` and `422` fail with an alert; `429`, `503
    unavailable`, other 5xx, timeouts and dropped connections retry after 5 s, 30 s and 2 min (with
    an injected clock), at most 3 times, honouring `Retry-After`; `500` retries once;
  - idempotency: a fresh UUID v4 per request, a new one for each chunk and each re-push; a retry
    sends the same key and a byte-identical body and gets the replay; the fake's conflict answer
    proves a rebuilt body would be caught; no retry once the key is 7 days old;
  - `changedSince` paging: every page is read, `status_since` moves only after the last page, a
    failed page keeps it; each status maps to its state; `opted_out`, from the sync and from a
    by-id refresh, writes an all-channel suppression with `source='cadence'`, and the People list
    and the person card then show "Opted out (Cadence)"; `not_found` leaves `sending` rows alone;
  - pacing: the fake never counts more than 30 push or 60 status requests in a minute;
  - suppression scope and channel outcomes, with email-only and call-only suppressions blocking;
    the firm status rules;
  - the API key absent from events, job output and exception text;
  - every seat (admin, owner, user) can push, and admin-only routes still refuse users;
  - with `CADENCE_API_URL` or `CADENCE_API_KEY` unset, the dialog explains that Cadence is not
    connected, nothing is sent, and People and the firm page render as before;
  - the daily cap.
- **Contract fixture.** The contract's example responses (14.3 `ping`, 14.8 push and 14.14 check, 14.12 status),
  copied verbatim from Cadence's plan into a test fixture and read by Bellwether's parser, and a
  request built by Bellwether checked against the 14.4 tables. The same fixture should live in
  Cadence's tests, so either side drifting fails a build.
- **`scripts/qa_pg.py`** (local Postgres):
  - the schema is created twice without error;
  - two connections claim one contact at once and exactly one wins;
  - two senders try to resend one due request and exactly one sends;
  - two connections reserving pace slots never get the same slot;
  - re-push works only from `rejected`, `failed` and `removed`;
  - the status sync moves states and writes opt-outs;
  - the Cadence filter's `EXISTS` uses the index.
- **`scripts/qa_smoke.py`**: against the local app and `scripts/fake_cadence.py`, covering:
  - preview; push one; push many across requests; partial refusal;
  - `401`; `429`; a timeout followed by a retry with the same key;
  - batch polling; a plain user can push; admin routes returning 403 for users.
- **`scripts/qa_html.py`, `scripts/qa_visual.py`**:
  - checkboxes, bar and dialog at 1440, 768 and 390 px;
  - keyboard: Esc closes, focus returns to the button, checkbox labels are read;
  - no horizontal overflow.
- **Live acceptance**: push one test contact, see it in Cadence's sandbox, move it to the CRM in
  Cadence, and see "Moved to CRM" in Bellwether within 10 minutes, or at once with Refresh.

### Risks

1. **Contract drift.** Mitigation: Shared contract with Bellwether copied verbatim, the shared
   fixture test, and the fake Cadence built from 14.3 to 14.12. Changes within v1 are additive only.
2. **Name-keyed ids are unstable.** An `n:` key changes if a website respells a name, which gives a
   second Bellwether id. Mitigation: dedupe by email and LinkedIn (see Dedupe), plus Cadence's own
   dedupe.
3. **A switchboard pushed as "the phone".** Mitigation: `phoneType` travels with every phone, and
   Cadence never matches on shared lines (14.6).
4. **Published emails can bounce.** They go as `emailVerification` `unknown`. Cadence should show
   it.
5. **Owner mismatch.** A Bellwether user with no Cadence account, or a different email in each app.
   Cadence then creates the contact unclaimed (14.7), and the result line says so.
6. **Opt-outs only in Cadence.** If the status sync is down, Bellwether may show a stale state, but
   the preview's check reports Cadence's refusal (`suppressed`, 14.6 rule 2) before anything is
   sent. Alerts fire after two hours of failed sync.
7. **A retry with a changed body.** If a retry rebuilt the payload, Cadence would answer `409
   idempotency_conflict`. Mitigation: the body is stored and resent byte for byte.
8. **Key leakage** through logs or errors. Mitigation: redaction and a test for it.
9. **Bulk misuse** filling the sandbox. Everyone can push (decision 16). Mitigation: per-push and
   daily caps, set by an admin, and the audit log.
10. **Reachability from `worker-5`.** Proven with Test connection before switching on.
11. **Stale index.** The payload is built from live tables at push time, not from `people_index`.
12. **The check and the push disagree.** The CRM can change between the preview and the push.
    Mitigation: the push's own rule 4 answers `already_in_crm`, which is recorded and shown the
    same way.

## Decisions (2026-11-03)

Rahul answered the open questions on 3 November 2026 (`Tech Projects/rahul-decisions-2026-11-03.md`)
and gave the go-ahead. In his numbering, questions 1 to 15 were Cadence's and 16 to 25 were
Bellwether's. He changed 16, 17, 23 and 25; for 18 to 22 and 24 the defaults stand. Each
question this plan asked is listed below with what is built, and Rahul's number where he named
the topic. Elsewhere in this plan, "decision 16" and the like use Rahul's numbers.

1. **Who may push? Changed (Rahul's 16).** Everyone can push: every seat, with no admin narrowing
   and no push-permission setting (`cadence.push_roles` is not built). Admins get nothing extra
   for pushing, so anyone may push again a contact Cadence `removed`.
2. **Who owns a pushed contact in the sandbox? Default.** The contract decides (14.7): the owner
   chosen in the dialog, else the pusher, if Cadence knows them and their Cadence role can own
   contacts; otherwise the contact is created unclaimed. The dialog defaults to the pusher and
   offers any Bellwether user with an email address, and an unclaimed result is shown as such.
3. **Firms marked Customer or Disqualified, and the CRM? Default, plus a CRM check (Rahul's
   17).** Customer blocks; Disqualified warns and needs a tick. Before a push, the preview calls
   Cadence's `contacts/check` (14.14). Any contact whose result is `already_in_crm` (its email or
   phone matches a CRM contact, or its LinkedIn URL; `matchedOn` says which) is blocked, shown as
   "Already in the CRM" with the matched field, and not sent. The push itself still handles `already_in_crm` the same way if the
   CRM changed in between.
4. **Contacts whose only phone is the firm's main line? Default.** Push them if they have an email
   or any phone, with the main line sent as `phoneType` `main`.
5. **Published but unconfirmed emails? Default.** Sent as `emailVerification` `unknown`, and
   catch-all addresses as `risky`. Never guesses.
6. **Same SEC-registered person at two firms? Default.** One contact; the second push is skipped as
   "already pushed from <firm>". Cadence also matches on `individualCrd` (14.6 rule 5).
7. **Re-pushing? Default, adjusted by Rahul's 16.** Anyone can push again a refused, failed or
   removed contact; an opted-out contact cannot be pushed again. Each re-push is a new request with
   a new key.
8. **Bellwether's do-not-contact list? Default.** Anyone can add; only admins lift (lifting is a
   setting, not a push); Cadence opt-outs arrive automatically.
9. **Erasure request via Cadence? Default.** No erasure signal in v1 (Contract gap 16).
   `opted_out` blocks every channel in Bellwether, and Bellwether does not hide anyone's emails and
   phones on its own. If Cadence adds a signal, Bellwether hides that person's emails and phones,
   blocks pushes, and keeps the public SEC roster row.
10. **Caps? Default.** 200 contacts per push, 500 per user per day.
11. **Details that improve after a push? Default.** No automatic update in this build; the card
    says "Newer details in Bellwether" when the best email or phone differs from what was sent. A
    re-push would only fill empty fields in Cadence (14.6 rule 5), so v1 does not offer one for a
    contact in the sandbox.
12. **Do not email and do not call, separately? Default.** A Bellwether email-only or call-only
    suppression blocks the push locally, the same as an all-channel one (Contract gap 15).
13. **ATS and Tollbooth? Changed (Rahul's 25).** Bellwether sends neither the tag `list:ATS` nor
    `TOLLBOOTH`, ever. ATS-list contacts are pushed with no product and no ATS tag.
14. **Opt-outs back from Cadence? Default, plus (Rahul's 23).** A contact opted out in Cadence
    shows as opted out in Bellwether. The `cadence_sync` job (every 10 minutes, `changedSince`) and
    the by-id refresh write a `contact_suppression` (all channels, `source='cadence'`), and the
    person card and the People list show "Opted out (Cadence)".

**Defaults taken during the build** (no question asked; written here so they can be changed):

- **The check that does not answer.** A timeout, connection error, `429` or 5xx on the preview's
  check lets the push go ahead with a note, because the push's own rule 4 still refuses anyone
  already in the CRM. A `401` or `503 integration_disabled` on the check disables the push. The
  preview never writes a hold; only a push or the sync does.
- **Contacts the check says Cadence would refuse** (`rejected`) are skipped like `already_in_crm`,
  with the reason in words. Sending them would only bring back the same refusal.
- **A preview's check answers are kept 15 minutes, in the web process that made them.** A push
  that reaches the other worker, or comes later, runs the check again first.
- **Check-blocked contacts get no `cadence_push` row.** They are recorded in the audit as
  `skipped:already_in_crm`, and the preview says so each time.
- **HTTPS only, except to this machine.** `CADENCE_API_URL` must be `https://`; plain `http` is
  accepted only for `127.0.0.1` and `localhost`, where the tests' fake Cadence listens.
- **ATS is not offered as a product** in the dialog. A push from the ATS list carries no product,
  and the designation tags leave out ATS's titles.
- **Retry in Settings › Cadence pushes in the name of the original pusher**, so an admin's retry
  does not make the admin the owner, and does not count against the admin's daily cap.
- **Do not contact on a card** covers the person or everyone at the firm, on all channels, email or
  calls, with a reason and a note. Email, phone and domain scopes exist in the table for Cadence's
  opt-outs and for later; a person's email suppression also shows on People.
- **The firm page's Overview copies of the first two cards** leave out the push controls; they
  live on the People & contacts tab.
- **Phones in the dialog** are shown the way the rest of the app writes them, `(212) 555-0142`,
  while the payload carries E.164.
- **A contact Cadence answers `not_found` by id** is marked failed ("Cadence has no record of it")
  unless a push of it is still in flight.
- **Local checks.** `scripts/qa_cadence.py` runs offline on SQLite and the fake (it is part of
  `scripts.qa_requirements`, so the Docker build runs it); `scripts/qa_pg.py` adds Postgres race
  checks; `scripts/qa_smoke.py --cadence-port <port>` (or `--only cadence` on a small database)
  drives the running app against the fake.
