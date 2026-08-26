"""Adversarial schema assertions.

The SEC changes column names and file layouts without notice and the failure is
silent: a renamed column becomes a null field, scores shift, and nobody notices
for three months. Everything here fails the run rather than proceeding with a
partial parse.

Header rows are also fingerprinted and stored on every pull, so when a layout
does change we can see exactly what it was before and when it moved.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from . import pg


class SchemaViolation(RuntimeError):
    """Upstream schema no longer matches what the config declares."""


def fingerprint(columns: list[str]) -> str:
    return hashlib.sha256("\x1f".join(columns).encode("utf-8")).hexdigest()


def record_columns(conn: pg.Connection, source_key: str, member: str,
                   columns: list[str], snapshot_id: int | None = None) -> str:
    """Store the observed header. Idempotent per distinct layout."""
    fp = fingerprint(columns)
    conn.execute(
        "INSERT OR IGNORE INTO source_schema"
        " (source_key, member, observed_at, columns_json, columns_sha256, snapshot_id)"
        " VALUES (?,?,?,?,?,?)",
        (source_key, member, datetime.now(timezone.utc).isoformat(timespec="seconds"),
         json.dumps(columns), fp, snapshot_id),
    )
    conn.commit()
    return fp


def known_layouts(conn: pg.Connection, source_key: str, member: str) -> list[list[str]]:
    rows = conn.execute(
        "SELECT columns_json FROM source_schema WHERE source_key=? AND member=?"
        " ORDER BY id",
        (source_key, member),
    ).fetchall()
    return [json.loads(r["columns_json"]) for r in rows]


def require_columns(observed: list[str], required: list[str], where: str) -> None:
    """Hard assertion. Missing required columns fail the run."""
    missing = [c for c in required if c not in observed]
    if missing:
        raise SchemaViolation(
            f"{where}: required columns absent upstream: {missing}. "
            f"Observed {len(observed)} columns: {observed[:25]}"
            f"{' ...' if len(observed) > 25 else ''}. "
            "Refusing to proceed with a partial parse."
        )


def warn_new_columns(observed: list[str], previous: list[str] | None) -> str | None:
    """Additive drift is reported, not fatal. Removal is caught by require_columns."""
    if not previous:
        return None
    added = [c for c in observed if c not in previous]
    dropped = [c for c in previous if c not in observed]
    if not added and not dropped:
        return None
    parts = []
    if added:
        parts.append(f"new columns upstream: {added}")
    if dropped:
        parts.append(f"columns disappeared upstream: {dropped}")
    return "; ".join(parts)


# --------------------------------------------------------------- XML assertions

def assert_xml_record(record, expected: dict, where: str) -> None:
    """Assert one parsed <Firm> carries the paths and attributes we depend on.

    Run against the first record of a feed. Cheap, and it catches a restructure
    before we write 23,000 rows of nulls.
    """
    for path in expected.get("required_paths", []):
        if record.find(path) is None:
            raise SchemaViolation(
                f"{where}: expected element '{path}' absent from record. "
                "Feed structure has changed upstream."
            )
    for path, attrs in (expected.get("required_attributes") or {}).items():
        el = record.find(path)
        if el is None:
            raise SchemaViolation(f"{where}: expected element '{path}' absent from record.")
        missing = [a for a in attrs if a not in el.attrib]
        if missing:
            raise SchemaViolation(
                f"{where}: element '{path}' missing attributes {missing}. "
                f"Present: {sorted(el.attrib)}. Refusing to proceed."
            )


# --------------------------------------------------- structural parse guards

class EmptyParse(RuntimeError):
    """A source that must contain rows parsed to none."""


def require_rows(n: int, where: str, hint: str = "") -> None:
    """Zero rows from a document that exists is a parse failure, not a result.

    Written after a namespace bug (<ns1:nameOfIssuer>) made roughly two thirds of
    13F information tables parse to zero. Nothing downstream could tell that
    apart from "this firm holds none of the target securities", so the overlay
    would have looked uniformly negative and the conclusion would have been that
    the signal does not exist. A wrong finding, not a broken run.

    Every filed information table has holdings; that is why it was filed.
    """
    if n <= 0:
        raise EmptyParse(
            f"{where}: parsed 0 rows from a document that exists. This is a parse "
            f"failure, not an empty result." + (f" {hint}" if hint else ""))


def require_all(rows, predicate, where: str, describe) -> None:
    """Fail the run on any row that violates a structural invariant.

    Used on form.idx, where a mis-parsed line yields a name fragment in the CIK
    column rather than an error. Proceeding with fragments is how a source
    silently degrades.
    """
    bad = [r for r in rows if not predicate(r)]
    if bad:
        sample = ", ".join(describe(r) for r in bad[:5])
        raise SchemaViolation(
            f"{where}: {len(bad)} of {len(rows)} rows failed a structural check. "
            f"Examples: {sample}. Refusing to proceed with fragments."
        )
