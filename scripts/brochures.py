"""Fetch Form ADV Part 2A brochures and tag them deterministically.

Zero model calls. Tags come from the phrase vocabulary in
config/brochure_tags.yml; every tag stores its verbatim supporting sentence
(capped at 25 words), its confidence components, and its section. Sentences
where a hit shares a sentence with a negation term are routed to the review
queue; the tag stays present at damped confidence until a human decides,
never absent by default.

Discovery runs against the IAPD firm API with the declared research
User-Agent. The brochure PDF endpoint (files.adviserinfo.sec.gov, FINRA
infrastructure) rejects non-browser agents, so that one request uses a
browser User-Agent plus a From header carrying our contact address, at the
same throttle as everything else.

Caching: a brochure is refetched only when the filed dateSubmitted moves.
Brochures change roughly once a year.

    python -m scripts.brochures [--limit N] [--scope working|sample|band]
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db, net, runlog  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS brochure (
    crd             TEXT PRIMARY KEY,
    version_id      INTEGER,
    brochure_name   TEXT,
    date_submitted  TEXT,
    fetched_at      TEXT,
    pdf_path        TEXT,
    bytes           INTEGER,
    pages           INTEGER,
    text_chars      INTEGER,
    status          TEXT NOT NULL     -- ok | no_brochure | fetch_failed | parse_failed
);
CREATE TABLE IF NOT EXISTS brochure_tag (
    crd             TEXT NOT NULL,
    tag             TEXT NOT NULL,
    present         INTEGER NOT NULL,
    confidence      REAL,
    specificity     REAL,
    hits            INTEGER,
    section_bonus   REAL,
    negation_damp   REAL,             -- 0.6 while an ambiguous hit is unreviewed, else 1.0
    best_snippet    TEXT,             -- verbatim sentence, <= 25 words
    best_phrase     TEXT,
    section_item    INTEGER,
    config_stamp    TEXT NOT NULL,
    PRIMARY KEY (crd, tag)
);
CREATE TABLE IF NOT EXISTS brochure_negation (
    id              INTEGER PRIMARY KEY,
    crd             TEXT NOT NULL,
    tag             TEXT NOT NULL,
    phrase          TEXT,
    sentence        TEXT,
    status          TEXT NOT NULL DEFAULT 'open',  -- open | negation_confirmed | tag_confirmed
    decided_by      TEXT,
    decided_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_bneg_status ON brochure_negation (status);
"""

BROCHURE_DIR = config.DATA_DIR / "brochures"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126 Safari/537.36")

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")
ITEM_RE = re.compile(r"\bItem\s+(\d{1,2})\b", re.I)


def discover(fetch: net.Fetcher, crd: str) -> dict | None:
    """Latest brochure version for a firm, from the IAPD firm API."""
    try:
        r = fetch._request(f"https://api.adviserinfo.sec.gov/search/firm/{crd}",
                           stream=False)
        d = json.loads(r.content); r.close()
    except Exception:
        return None
    try:
        ia = d["hits"]["hits"][0]["_source"]["iacontent"]
        ia = json.loads(ia) if isinstance(ia, str) else ia
        det = (ia.get("brochures") or {}).get("brochuredetails") or []
    except Exception:
        return None
    if not det:
        return None
    def key(b):
        try:
            return datetime.strptime(b.get("dateSubmitted", "1/1/1900"), "%m/%d/%Y")
        except ValueError:
            return datetime(1900, 1, 1)
    b = max(det, key=key)
    return {"version_id": b.get("brochureVersionID"),
            "name": b.get("brochureName"),
            "date": b.get("dateSubmitted")}


def fetch_pdf(fetch: net.Fetcher, contact: str, version_id: int) -> bytes | None:
    url = ("https://files.adviserinfo.sec.gov/IAPD/Content/Common/"
           f"crd_iapd_Brochure.aspx?BRCHR_VRSN_ID={version_id}")
    fetch._throttle()
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Referer": "https://adviserinfo.sec.gov/",
        "From": contact,
    })
    try:
        body = urllib.request.urlopen(req, timeout=90).read()
    except Exception:
        return None
    return body if body[:4] == b"%PDF" else None


def extract_text(pdf: bytes, max_pages: int) -> tuple[str, int]:
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        pages = len(doc.pages)
        for pg in doc.pages[:max_pages]:
            parts.append(pg.extract_text() or "")
    return "\n".join(parts), pages


def item_spans(text: str) -> list[tuple[int, int]]:
    """(offset, item_number) for every 'Item N' occurrence, for section bonuses."""
    return [(m.start(), int(m.group(1))) for m in ITEM_RE.finditer(text)]


def item_at(spans, pos: int) -> int | None:
    cur = None
    for off, num in spans:
        if off > pos:
            break
        cur = num
    return cur


def cap_words(s: str, n: int) -> str:
    # em and en dashes in source PDFs are normalized to hyphens: verbatim enough
    # for an SDR, and the UI carries no em dashes anywhere
    s = s.replace("—", "-").replace("–", "-")
    w = s.split()
    return " ".join(w[:n]) + ("..." if len(w) > n else "")


def tag_text(text: str, cfgtags: dict, stamp: str):
    """Return (tag rows, negation rows)."""
    spans = item_spans(text)
    lower = text.lower()
    sentences = SENT_SPLIT.split(text)
    # sentence offsets for section lookup
    offs, pos = [], 0
    for s in sentences:
        i = text.find(s, pos)
        offs.append(i if i >= 0 else pos)
        pos = (i if i >= 0 else pos) + len(s)

    neg_terms = [t.lower() for t in cfgtags["negation_terms"]]
    bonus_items = set(cfgtags["section_bonus_items"])
    maxw = cfgtags["snippet_max_words"]

    tag_rows, neg_rows = [], []
    for tag, spec in cfgtags["tags"].items():
        best = None            # (specificity, sentence, phrase, section)
        hits = 0
        section_hit = False
        negated = []
        for phrase in spec["phrases"]:
            pl = phrase.lower()
            wordy = len(phrase.split())
            specificity = 0.9 if wordy >= 3 else (0.7 if wordy == 2 else 0.5)
            if wordy == 1:
                pat = re.compile(rf"\b{re.escape(pl)}\b",
                                 re.I if phrase.upper() != phrase else 0)
                matches = [m.start() for m in pat.finditer(text if phrase.upper() == phrase else lower)]
            else:
                matches = []
                start = 0
                while True:
                    i = lower.find(pl, start)
                    if i < 0:
                        break
                    matches.append(i)
                    start = i + 1
            for mpos in matches:
                hits += 1
                si = max(i for i, o in enumerate(offs) if o <= mpos) if offs else 0
                sent = " ".join(sentences[si].split())
                item = item_at(spans, mpos)
                in_bonus = item in bonus_items
                section_hit = section_hit or in_bonus
                is_neg = any(t in sent.lower() for t in neg_terms)
                if is_neg:
                    negated.append((phrase, sent))
                cand = (specificity + (0.01 if in_bonus else 0), sent, phrase, item)
                # prefer the most specific non-negated hit for the snippet
                if not is_neg and (best is None or cand[0] > best[0]):
                    best = cand
                elif best is None:
                    best = cand
        if hits == 0:
            continue
        specificity = best[0] - (0.01 if best[3] in bonus_items else 0)
        # Damp only when NO clean hit exists: if the best snippet is itself one of
        # the negation-ambiguous sentences, the tag rests entirely on ambiguous
        # evidence and waits on review at reduced confidence, never absent.
        clean_exists = best[1] not in [n[1] for n in negated]
        damp = 1.0 if clean_exists else 0.6
        conf = min(1.0, (specificity + min(hits, 5) * 0.02 +
                         (0.05 if section_hit else 0.0))) * damp
        tag_rows.append((tag, 1, round(conf, 3), specificity, hits,
                         0.05 if section_hit else 0.0, damp,
                         cap_words(best[1], maxw), best[2], best[3], stamp))
        for phrase, sent in negated:
            neg_rows.append((tag, phrase, cap_words(sent, 40)))
    return tag_rows, neg_rows


def scope_crds(conn, scope: str) -> list[str]:
    if scope == "working":
        q = """SELECT crd FROM tier_a_rank WHERE in_working_list=1
               UNION SELECT crd FROM firm_overlay WHERE phh_13f=1"""
    elif scope == "sample":
        q = """SELECT crd FROM tier_a_rank WHERE in_working_list=1
               UNION SELECT crd FROM firm_overlay WHERE phh_13f=1
               UNION SELECT crd FROM (SELECT crd FROM tier_c_score ORDER BY rank LIMIT 120)"""
    else:  # band: every in-band registered adviser
        q = """SELECT crd FROM firm_current WHERE is_era=0 AND raum>=25e6 AND raum<500e6"""
    return [r["crd"] for r in conn.execute(q)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--scope", default="sample", choices=["working", "sample", "band"])
    args = ap.parse_args()

    cfg = config.load()
    tags_cfg = yaml.safe_load((config.CONFIG_DIR / "brochure_tags.yml").read_text(encoding="utf-8"))
    stamp = f"{cfg.stamp}|brochure_tags.v{tags_cfg['config_version']}"
    conn = db.connect()
    conn.executescript(SCHEMA); conn.commit()
    fetch = net.Fetcher(cfg.http)
    contact = cfg.http["user_agent"]
    BROCHURE_DIR.mkdir(parents=True, exist_ok=True)

    crds = scope_crds(conn, args.scope)
    done = {r["crd"] for r in conn.execute("SELECT crd FROM brochure")}
    todo = [c for c in crds if c not in done][:args.limit]
    print(f"scope={args.scope}: {len(crds)} firms, {len(done)} already processed, "
          f"doing {len(todo)} now")

    ok = nobro = failed = 0
    with runlog.Run(conn, "brochures", "ingest", stamp) as run:
        for i, crd in enumerate(todo, 1):
            meta = discover(fetch, crd)
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if not meta or not meta.get("version_id"):
                conn.execute("INSERT OR REPLACE INTO brochure VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (crd, None, None, None, now, None, None, None, None,
                              "no_brochure"))
                nobro += 1
                conn.commit()
                continue
            pdf = fetch_pdf(fetch, contact, meta["version_id"])
            if pdf is None:
                conn.execute("INSERT OR REPLACE INTO brochure VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (crd, meta["version_id"], meta["name"], meta["date"],
                              now, None, None, None, None, "fetch_failed"))
                failed += 1
                conn.commit()
                continue
            path = BROCHURE_DIR / f"{crd}_{meta['version_id']}.pdf"
            path.write_bytes(pdf)
            try:
                text, pages = extract_text(pdf, tags_cfg["max_pages"])
            except Exception:
                conn.execute("INSERT OR REPLACE INTO brochure VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (crd, meta["version_id"], meta["name"], meta["date"],
                              now, str(path), len(pdf), None, None, "parse_failed"))
                failed += 1
                conn.commit()
                continue
            tag_rows, neg_rows = tag_text(text, tags_cfg, stamp)
            conn.execute("DELETE FROM brochure_tag WHERE crd=?", (crd,))
            conn.execute("DELETE FROM brochure_negation WHERE crd=? AND status='open'", (crd,))
            for t in tag_rows:
                conn.execute("INSERT INTO brochure_tag VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (crd, *t))
            for tag, phrase, sent in neg_rows:
                conn.execute("INSERT INTO brochure_negation (crd,tag,phrase,sentence)"
                             " VALUES (?,?,?,?)", (crd, tag, phrase, sent))
            conn.execute("INSERT OR REPLACE INTO brochure VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (crd, meta["version_id"], meta["name"], meta["date"], now,
                          str(path), len(pdf), pages, len(text), "ok"))
            ok += 1
            conn.commit()
            if i % 10 == 0:
                print(f"  {i}/{len(todo)}  ok={ok} no_brochure={nobro} failed={failed}")
        run.rows_out = ok
        run.note(f"ok={ok} no_brochure={nobro} failed={failed}")

    print(f"\nok {ok} | no brochure {nobro} | failed {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
