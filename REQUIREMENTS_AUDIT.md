# Bellwether requirements audit

Reviewed 5 October 2026 against the 15 requested improvements. The overall
result is **partially achieved**, not a complete production acceptance.

The live site at https://bellwether.pmx.acumen-strategy.com/ responds and redirects
to its login page. Its login currently offers passwords only: Microsoft sign-in
is not configured. Authenticated production data, provider settings and job
history were not accessible during this audit.

| # | Requirement | Finding |
| --- | --- | --- |
| 1 | Named emails, phones and custom sources | Implemented: team/bio pages, vCards, structured people, role-inbox classification, directory matching, source schedules and custom-adapter warnings. Firm websites have a separate tab. Extraction works on controlled fixtures; real-world coverage needs a production sample. This is public-source discovery, so an arbitrary directory cannot be guaranteed to yield contacts. |
| 2 | Reuse `site_email_harvester.py` | `prospect/harvest.py` credits and implements the port: filtering, Cloudflare decoding, obfuscation handling and confidence. The original standalone file is absent from this checkout, so exact parity cannot be verified. |
| 3 | Ranked lists and editable scoring with missing-data warnings | One ranked list per product; missing factors earn zero, with coverage and potential recorded. Weights, bands, factors and seat restrictions are implemented. Found and fixed built-in level edits being stored without affecting evaluator points. Built-in condition logic still lives in Python; editing a condition's description does not change its logic. Numeric field factors support editable thresholds. |
| 4 | Scrapling | Installed and used by the crawler. Actual static fetch, contact extraction and browser-rendered fetch passed against a local HTTP fixture. Production browser installation and source success rates remain to be checked. |
| 5 | AI for questions, enrichment and cleaning | Anthropic, Eden AI and compatible endpoints are wired into questions, briefs, extraction and title cleaning. Eden transport tested with a stub provider response. Production key/model availability and answer quality are unverified. The current daily quota is a count-before-call check; simultaneous calls can exceed it, so it is not a strict atomic spending cap. |
| 6 | Microsoft login and seats | MSAL flow, tenant/domain validation, Rahul's admin assignment, admin/owner/user seats and product-family permissions are implemented and tested locally. **Microsoft is not active on the live login.** Entra registration and tenant/client/secret settings are still required. |
| 7 | Admin-only Settings | Navigation and server-side authorization enforce this. Plain-user route and write-denial checks passed. |
| 8 | Automatic scheduled jobs | Startup scheduler and worker handle due jobs, backlogs, pause and forced runs. Fixed failed jobs ignoring their retry delay and the Jobs page crashing on an unavailable progress count. Scheduling decisions and controls tested; sustained production execution is unverified. |
| 9 | Firm intelligence, people, hiring, AI; remove Call prep | Dossier includes people, contacts, hiring/departures, scoring evidence, signals, assets, investments, technology and compliance. Call prep is absent. Converted the long page into accessible tabs, preserving deep links and all sections. Hiring data primarily represents registered-adviser employment changes, not a complete employee census or a comprehensive job-vacancy feed. |
| 10 | Useful Home dashboard | Real-data aggregates, product rankings, coverage, activity, hiring, geography and pipeline are implemented. Removed promotional/explanatory paragraphs and repeated suggestions. Unavailable AI is represented as name search. Empty background activity no longer falsely claims everything is caught up. |
| 11 | Reacher, individual and bulk verification | Reacher adapter, native SMTP fallback, bulk interface and individual/firm checks exist. Fixed optimistic Reacher results: verified requires explicit mailbox acceptance and a negative catch-all test. DNS-only results remain unknown. No production mailbox tests were performed; Reacher endpoint/authentication and outbound SMTP availability need confirmation. Previously stored results are not rewritten by this code change; reverify them to apply the stricter rule. |
| 12 | Smooth performance | Shared caching, pooling and background jobs exist. Local route and concurrent-load checks passed on a small synthetic dataset. Tabs switch without a page reload; mobile navigation and contained tables are usable. Production-scale latency remains unmeasured. |
| 13 | Thinking orbs | MIT engine is vendored with attribution and wired into login and AI surfaces. Browser rendering works, with reduced-motion and offscreen/hidden-page pausing. |
| 14 | Verify integrations and functionality | PostgreSQL adapter checks, 21 offline regressions, authenticated route/role/write/load checks, HTML checks and desktop/mobile browser checks completed. Provider billing, real Microsoft login and real-source coverage are explicitly outside what these local tests prove. |
| 15 | GitHub and live deployment | Existing main branch is connected to Harbor/Nomad through GitHub Actions. Deployment status for this change is recorded in the delivery message; repository presence alone is not evidence of successful deployment. |

## Changes from this audit

- Near-black cool surfaces, restrained red actions, readable secondary text and
  sans-serif interface headings. The wordmark retains its existing serif.
- Concise Home, People, Firms, Enrichment, AI and login screens; detailed help
  available on demand. Login remains centered.
- Firm tabs with keyboard navigation, hash links and browser history; all sections
  remain available without JavaScript. Mobile menu and contained table scrolling.
- Fixed contact-save and corrected-website buttons that previously did nothing
  when clicked. Background actions now report HTTP failures correctly.
- Scoring edits affect actual built-in points. Missing-data coverage is visible
  beside the firm header's score badges and dashboard top-firm scores.
- Failed-job retry delay, incomplete job-progress rendering, and strict email
  verification semantics corrected.
- Docker builds now run compilation, copy-style checks and offline regressions
  before publishing an image.

## Validation boundaries

Runtime tests used an isolated local PostgreSQL 16 database and eight synthetic
firms. Production records and the local legacy SQLite database were not changed.
The repository's user-supplied `Product scores/` documents were left untouched.
Screenshots and temporary QA data are under ignored `data/` and are not shipped.

The authenticated smoke suite exercises reads, exports, notes, owners, score
overrides, scoring persistence/reset, saved views, watch lists and job controls.
Its concurrent pass completed 200 requests while an ingest transaction wrote.
Browser checks cover 1440px desktop, 768px tablet and 390px mobile, contact submit,
corrected URL submit, hash-linked tabs, keyboard selection and menu controls.
The corrected-URL browser check intercepts its request; the separate Scrapling
test executes the actual fetch and render paths against localhost.

## Remaining production acceptance

### Charcoal UI and firm chat follow-up (2026-10-05)

- Replaced blue-tinted neutrals throughout the app with neutral charcoal.
  The centered login now uses a larger Thinking Orb, orbital linework and
  restrained red illumination. Reduced-motion behavior is preserved.
- Firm pages now lead with a name/monogram identity and an overview containing
  product fit with missing-factor counts, filing facts, asset history when
  available, people and recent signals. Status editing is collapsed beside
  notes and lists. All existing detailed tabs remain accessible.
- Promoted firm-scoped AI to the first side panel and added a header shortcut.
  Questions retain context for follow-ups during the current page session.
  Pending submissions are locked, failures preserve the question with retry,
  and expired sessions and timeouts have explicit errors. Unavailable firm AI
  no longer silently searches the whole database. Invalid firm scope is rejected.
- Home has clearer hierarchy and a collapsible geography view, reducing empty
  vertical space without removing data. Narrow-screen search and settings fields
  no longer overflow. Clearing a firm's status no longer crashes its page.
- Validation: 24 offline regressions; authenticated smoke suite including role,
  export, write and scoring checks; 200/200 concurrent requests while ingest
  wrote; browser interaction and overflow checks from 320px through 1440px.
  Firm-chat tests cover unavailable-provider responses, mocked successful replies,
  follow-up history, duplicate prevention, recoverable HTTP errors, context
  isolation between firms and malformed API requests. No browser JavaScript errors.
- These tests used synthetic local records. Successful model answers were mocked;
  production AI configuration and answer quality still require live acceptance.
  Conversations are not persisted after navigation or reload. The production
  integration acceptance items below remain outstanding.

1. Register Microsoft Entra's Web callback as
   `https://bellwether.pmx.acumen-strategy.com/auth/microsoft/callback` and configure
   the tenant ID, client ID and secret in Settings. Complete real sign-in as Rahul
   and a product owner; verify their respective permissions.
2. Confirm the AI provider, permitted model IDs and budget; run a firm question,
   brief, extraction and cleaning task and inspect the evidence.
3. Confirm Reacher health and SMTP egress. Test known valid, invalid and catch-all
   addresses in individual and bulk modes; refresh previous optimistic results.
4. Sample representative firms and added directories. Measure named contact
   coverage and matching accuracy; commission adapters for flagged sources.
5. Check production worker heartbeat, completed schedules, backlog progress and
   page latency on the full dataset after deployment.

Upstream references: [Scrapling](https://github.com/D4Vinci/Scrapling),
[Reacher](https://github.com/reacherhq/check-if-email-exists),
[Thinking Orbs](https://github.com/Jakubantalik/thinking-orbs).
