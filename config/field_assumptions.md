# Field meaning register

Every join, filter and type coercion in this system fails loudly. Every
field-meaning assumption fails silently. `Q5K3` was the first: the column name
said custodians, the data said something narrower, and the gate built on it
dropped 4,137 in-band firms without an error.

So for each field currently driving a score, filter or classification: what it is
believed to mean, and how that belief was tested. Where the test column reads
**assumed**, the only evidence is the column name, and it sits in the same risk
class as `Q5K3` did before anyone checked.

Update this file whenever a field starts driving a decision.

---

## Verified against independent ground truth

| Field | Believed meaning | How verified | Result |
|---|---|---|---|
| `Info/@FirmCrdNb` | Firm CRD number | Joined 1,954,043 Schedule D rows through the Base_A crosswalk | 100% resolution, zero orphans |
| Base_A `1E1` | CRD (Item 1.E(1)) | Same join | 471,129 pairs, no collisions |
| `Item5F/@Q5F2C` | Total regulatory AUM | Arithmetic identity: `Q5DA3 + Q5DB3 = Q5F2C` and `Q5F2A + Q5F2B = Q5F2C` on sampled records | Exact to the dollar |
| `Item5D/@Q5DB1`, `@Q5DB3` | High net worth client count and AUM | Same identity; category ordering A=non-HNW individuals, B=HNW confirmed by the sums | Consistent |
| `Item5D/@Q5DA1`, `@Q5DA3` | Non-HNW individual clients and AUM | Same | Consistent |
| `Item7B/@Q7B` | Firm advises at least one private fund | Tested against presence of Schedule D 7B1 rows on each firm's latest post-2018 filing, n=21,987 | **recall 100%, precision 95%** |
| `Rgstn/@FirmType` | Registered adviser vs exempt reporting adviser | Behavioural: all 6,638 ERAs carry null RAUM and all answer `Q7B=Y` | Consistent with the ERA definition |
| `sched_d_7b1.Fund Type` | Filed fund classification | Closed vocabulary of exactly 7 values matching the Form ADV instruction set | No free text, no drift |
| `sched_d_7b1.Gross Asset Value` | Fund gross asset value in dollars | Summed per filing and compared with Item 5.D pooled-vehicle AUM (`5D3f`), which the Form ADV instructions define as the fund's gross asset value. n=76,709 filings | **median ratio 1.000, Spearman 0.918**, deciles 4-8 all exactly 1.00 |
| `sched_d_7b1.Owners` | Number of investors in the fund | Compared with Form D totalNumberAlreadyInvested for 128 matched real estate funds (112 both nonzero) | **Spearman 0.733, median ADV/FormD ratio 1.12, ADV >= FormD in 77.7%** which is the expected direction since ADV is amended annually and funds grow after the Form D snapshot. Large divergences trace to old initial Form Ds. Verified |
| `sched_d_7b1.Minimum Investment` | Minimum subscription | Cross-checked against Form D minimumInvestmentAccepted on 69 matched funds | **58% exactly equal, median ratio 1.00.** Verified |
| `Item5B/@Q5B1` | Investment adviser representative count | Two consistency tests: IAR must not exceed Item 5.A total employees; feed value vs archive `5B1` | **0 violations in 17,104 firms**; 87.4% agree within 25% or 2 reps across a 1-3 year gap. Accepted as verified: the individual adviser feed would be definitive but is not worth a 176 MB ingest for a field driving a secondary trigger |

## Measured non-issues

- The 13F coverage measurement (tier C 38.5 to 41.4%) was first computed on an
  index whose company names were truncated at 62 chars by a mis-parsed form.idx.
  Re-measured on the clean index: deltas at or under 0.2pp on every population.
  The 15% build threshold decision was robust to a data error nobody knew about.
  Recorded so the corrupted-era figure is not re-litigated.

## Tested and disproven

These do **not** mean what their names suggest. Neither is used in any score.

| Field | Name suggests | Actually | Evidence |
|---|---|---|---|
| `Item5K/@Q5K3` | Firm reports a 10%+ SMA custodian | A narrower question, most likely related-person custody | **recall 19.7%**, precision 93.5% against Schedule D 5K3 row presence. 4,137 in-band firms report custodians while answering N |
| `Item5C/@Q5C1` | Total client count | Something else | Reads `0` on records whose Item 5.D categories sum to 82 clients. Total clients is summed from 5.D instead |

## Assumed: column name only, untested

These drive live decisions on nothing but their label. Listed worst first by how
much weight they carry.

| Field | Believed meaning | Drives | Why it is untested |
|---|---|---|---|
| `sched_d_5k3.5K(3)(g)` | Assets held at that custodian | **The entire Schwab share figure**, shown on inbox, list and detail | No independent source in hand. Cannot be cross-footed against Item 5.F because SMA assets are not RAUM, and Schedule D 5.K(1), which carries the SMA total, is not ingested |
| `sched_d_7b1.Minimum Investment` | Minimum subscription | Shown on detail and call prep as a PHH qualifier | No second source |
| `Item5A/@TtlEmp` | Total employees | Displayed on detail | Testable against Base_A `5A`. Not yet done |
| `Item11/@Q11` | Discloses a disciplinary event | Exclusion flag, shown as a red warning on detail | Testable against the four DRP tables in archive part 1. Not yet done |
| `MainAddr/@State` | Office state | State filter on the firm list | Low risk, but 2,273 registered firms have no state at all |

### What is left

`Gross Asset Value` and `Q5B1` have moved to verified. Two load-bearing entries
remain untested:

- **`5K(3)(g)`** drives the entire Schwab share figure and has **no second source
  available**. Schedule D 5.K(1) carries the SMA total that would let it be
  cross-footed, and it is not published in a form we hold. The honest position is
  to leave it untested and make sure the caveat travels with the number
  everywhere it is displayed, which it now does: inbox, firm list, firm detail
  and the copied call-prep text.
- **`Owners`**: RESOLVED 2026-08-15 via Form D (see verified table). No
  load-bearing untested entries remain, so brochure work is unblocked.

`TtlEmp` and Item 11 remain assumed but neither drives a score: employees is
display-only and Item 11 is an exclusion flag whose false-negative cost is low.
Both are testable against archive part 1 when convenient.

**Brochure work does not start until this section is empty of load-bearing
entries.**
