"""Reclaim disk that has stopped earning its keep.

Bellwether caches a lot on purpose: refetching 8,000 brochures or 12,000 websites
is days of polite crawling, so a cached copy is worth real money in time. But
some of it stops being worth anything once the useful content has been extracted
into the database, and left alone it only grows.

What this reclaims, in order of how obviously safe it is:

  logs          rotated away; they are diagnostics, not records
  adv_pdfs      never read after parsing. The custodian names went into
                firm_refresh and nothing ever opens the PDF again
  web_html      cached HTML for pages that yielded no contact at all. The
                web_page row stays, so the crawler still knows never to refetch
                it; only the bytes go
  brochures     PDFs whose tags AND contacts have both been extracted. This one
                is off by default, because re-tagging with an improved phrase
                vocabulary would mean refetching. Ask for it explicitly.

Nothing here touches data/snapshots. Those are the immutable provenance of every
derived number and the promise that any past score can be recomputed exactly.

    python -m scripts.prune                  # report only, delete nothing
    python -m scripts.prune --apply
    python -m scripts.prune --apply --brochures
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prospect import config, db  # noqa: E402


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} GB"


def size_of(paths) -> int:
    total = 0
    for p in paths:
        try:
            total += Path(p).stat().st_size
        except OSError:
            pass
    return total


def find_logs() -> list[Path]:
    return [p for p in config.DATA_DIR.glob("*.log") if p.stat().st_size > 0]


def find_adv_pdfs() -> list[Path]:
    d = config.DATA_DIR / "adv_pdfs"
    return sorted(d.glob("*.pdf")) if d.exists() else []


def find_barren_web_html(conn) -> list[Path]:
    """Cached pages that produced no contact. The row stays; the bytes go."""
    rows = conn.execute("""
        SELECT cache_path FROM web_page
        WHERE cache_path IS NOT NULL
          AND url NOT IN (SELECT source_url FROM web_contact)""").fetchall()
    return [Path(r["cache_path"]) for r in rows]


def find_spent_brochures(conn) -> list[Path]:
    """PDFs that have been both tagged and scanned for contacts."""
    rows = conn.execute("""
        SELECT b.pdf_path FROM brochure b
        WHERE b.status='ok' AND b.pdf_path IS NOT NULL
          AND b.crd IN (SELECT crd FROM contact_scan)
          AND EXISTS (SELECT 1 FROM brochure_tag t WHERE t.crd=b.crd)""").fetchall()
    return [Path(r["pdf_path"]) for r in rows]


def clear_cache_paths(conn, paths: list[Path]) -> None:
    """Null the cache_path so nothing later tries to read a deleted file."""
    conn.executemany("UPDATE web_page SET cache_path=NULL WHERE cache_path=?",
                     [(str(p),) for p in paths])
    conn.commit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without this it only reports")
    ap.add_argument("--brochures", action="store_true",
                    help="also drop brochure PDFs already tagged and scanned")
    args = ap.parse_args()

    conn = db.connect()
    groups: list[tuple[str, list[Path], str]] = [
        ("logs", find_logs(), "diagnostics, not records"),
        ("adv_pdfs", find_adv_pdfs(), "never read after parsing"),
        ("web_html (no contact found)", find_barren_web_html(conn),
         "crawler still remembers the page; only the bytes go"),
    ]
    if args.brochures:
        groups.append(("brochures (tagged and scanned)", find_spent_brochures(conn),
                       "re-tagging later would need a refetch"))
    else:
        spent = find_spent_brochures(conn)
        print(f"not included (pass --brochures to include): "
              f"{len(spent):,} spent brochure PDFs, {human(size_of(spent))}\n")

    total = 0
    for name, paths, why in groups:
        n = size_of(paths)
        total += n
        print(f"  {name:32s} {len(paths):>7,} files  {human(n):>10s}   {why}")
    print(f"\n  {'reclaimable':32s} {'':>7s}        {human(total):>10s}")

    if not args.apply:
        print("\nReport only. Re-run with --apply to delete.")
        return 0

    removed = failed = 0
    for name, paths, _ in groups:
        web = name.startswith("web_html")
        gone: list[Path] = []
        for p in paths:
            try:
                p.unlink()
                gone.append(p)
                removed += 1
            except OSError:
                failed += 1
        if web and gone:
            clear_cache_paths(conn, gone)

    # Drop the directory too if we emptied it.
    d = config.DATA_DIR / "adv_pdfs"
    if d.exists() and not any(d.iterdir()):
        d.rmdir()

    print(f"\ndeleted {removed:,} files, reclaimed about {human(total)}"
          + (f", {failed} could not be removed (in use)" if failed else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
