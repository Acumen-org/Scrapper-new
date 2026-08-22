# CHECKPOINT X (2026-08-21)

Marker for "summarize everything since checkpoint X". State of the system at this
point, so later work can be diffed against it.

The tool was named **Bellwether** on 2026-08-22, after this checkpoint. Anything
below calling it the prospect engine is the same system under its old name.

## Built and working at this checkpoint
- Immutable snapshot store: weekly SEC adviser feed (1 capture), Schedule D
  archive 2011-2024, Filing-to-CRD crosswalk, all hashed and registered.
- Firm table: 23,743 firms (17,105 registered, 6,638 ERA, labeled not dropped).
- Schedule D history: 302,077 custodian rows, 1,651,966 private fund rows,
  471,129 filing-to-CRD pairs, all dated.
- Custodian alias table (51 entities, 90.7% rows mapped), TDA-to-Schwab
  migration suppressed as a dated config rule.
- Archive triggers: 15,539 events (custodian change split by direction,
  first private fund, first RE fund), recency-decayed, base rates computed.
- Segmentation: 190 in-band RE-fund firms -> prospect 37-40 / competitor / sponsor 139
  / ambiguous, RAUM ratio primary, HNW gate (share OR $50M absolute).
- Tier A (PHH): 360 firms ranked, top-100 working list, own-vehicle penalty applied.
- Tier C (AcuBooth): 5,399 scored, clients-per-rep paired with HNW-per-client damping.
- 13F: filer index 4 quarters (35,093 filings), CUSIP map 13/14 verified by
  observation (XYLD excluded), 3,421 filings ingested with 0 parse failures,
  2,419 target holdings, overlays cross-cutting.
- THE INTERSECTION: 21 tier A firms holding net lease names, sorted by position
  value with legacy/deliberate conviction flags. Chesapeake and WestEnd on top.
- Match table: 1,066 CIK-CRD links (auto/review/rejected) with confidence.
- UI: trigger inbox live (filters, actions, caveat tooltips). /health as JSON.
- Field register: everything verified or disproven except Owners (pending Form D).
- Identifier type audit: 27 columns, all configs, all joins clean after the YAML
  CUSIP-as-integer bug.

## Table counts at checkpoint
- snapshot: 3
- run_log: 33
- firm: 23,743
- filing_crd: 471,129
- sched_d_5k3: 302,077
- sched_d_7b1: 1,651,966
- trigger_event: 15,539
- trigger_action: 1
- re_segment: 190
- firm_custodian_profile: 13,578
- tier_a_rank: 360
- tier_c_score: 5,399
- edgar_13f_filer: 35,093
- adv_13f_match: 1,066
- holding_13f: 2,419
- filing_13f: 3,421
- firm_overlay: 341

## Pending at this checkpoint (the work that follows)
1. Form D ingest: verify Owners (last load-bearing register entry), seed competitor intel.
2. Brochure fetch + deterministic tagging (no LLM), negation review routing.
3. UI: firm detail with call-prep copy, unified review queue, pipeline health view.
4. Forward-looking triggers (new registration with cross-checks, AUM jump, IAR growth),
   live once a second weekly snapshot exists (feed published 08/13, next ~08/20).
5. Quarterly CUSIP re-verification as an executed control, geography views, CRM CSV.
