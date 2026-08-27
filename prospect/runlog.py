"""Run records for the pipeline health view.

Every run is written before the work starts and updated when it ends, so a
crashed process leaves a 'running' row rather than no evidence at all. Failure
is loud by construction: the context manager records the exception and re-raises.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone

from . import pg


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Run:
    """Context manager wrapping one source/stage execution."""

    def __init__(self, conn: pg.Connection, source_key: str, stage: str,
                 config_stamp: str):
        self.conn = conn
        self.source_key = source_key
        self.stage = stage
        self.config_stamp = config_stamp
        self.id: int | None = None
        self.rows_in: int | None = None
        self.rows_out: int | None = None
        self.flagged = False
        self.skipped = False
        self.message: str | None = None

    def skip(self, message: str) -> None:
        """Mark as deliberately skipped. Distinct from 'ok' in the health view:
        a skipped run did no work, and must not read as a successful pull."""
        self.skipped = True
        self.message = message

    def __enter__(self) -> "Run":
        cur = self.conn.execute(
            "INSERT INTO run_log (source_key, stage, started_at, status, config_stamp)"
            " VALUES (?,?,?,'running',?) RETURNING id",
            (self.source_key, self.stage, _now(), self.config_stamp),
        )
        self.id = cur.lastrowid
        self.conn.commit()
        return self

    def note(self, message: str) -> None:
        self.message = message

    def flag(self, message: str) -> None:
        self.flagged = True
        self.message = message if not self.message else f"{self.message}; {message}"

    def previous_rows_out(self) -> int | None:
        row = self.conn.execute(
            "SELECT rows_out FROM run_log"
            " WHERE source_key=? AND stage=? AND status='ok' AND rows_out IS NOT NULL"
            "   AND id <> ?"
            " ORDER BY id DESC LIMIT 1",
            (self.source_key, self.stage, self.id),
        ).fetchone()
        return row["rows_out"] if row else None

    def check_row_delta(self, rows_out: int, warn_pct: float,
                        min_plausible: int | None = None) -> None:
        """Compare against the last good run and flag movement in either direction.

        Flags rather than fails. A schema change fails the run; a volume change is
        a signal a human should look at, which is what the pipeline health view is
        for. Both directions matter: a source that silently doubles is as
        suspicious as one that halves.
        """
        self.rows_out = rows_out
        prev = self.previous_rows_out()
        pct = None
        if prev:
            pct = (rows_out - prev) / prev * 100.0
            if abs(pct) > warn_pct:
                direction = "up" if pct > 0 else "down"
                self.flag(
                    f"row count {direction} {abs(pct):.1f}% versus previous run "
                    f"({prev} -> {rows_out}), threshold {warn_pct}%"
                )
        if min_plausible is not None and rows_out < min_plausible:
            self.flag(f"row count {rows_out} below plausible floor {min_plausible}")
        self.conn.execute(
            "UPDATE run_log SET prev_rows_out=?, pct_change=? WHERE id=?",
            (prev, pct, self.id),
        )
        self.conn.commit()

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            # A failed statement aborts the whole Postgres transaction, so the
            # bookkeeping UPDATE below cannot run until it is rolled back --
            # and the InFailedSqlTransaction it would raise masks the real error.
            try:
                self.conn.rollback()
            except Exception:
                pass
            detail = "".join(traceback.format_exception_only(exc_type, exc)).strip()
            msg = f"{self.message}; {detail}" if self.message else detail
            self.conn.execute(
                "UPDATE run_log SET finished_at=?, status='failed', message=?,"
                " rows_in=?, rows_out=?, flagged=1 WHERE id=?",
                (_now(), msg, self.rows_in, self.rows_out, self.id),
            )
            self.conn.commit()
            return False  # never swallow: fail loudly
        self.conn.execute(
            "UPDATE run_log SET finished_at=?, status=?, message=?,"
            " rows_in=?, rows_out=?, flagged=? WHERE id=?",
            (_now(), "skipped" if self.skipped else "ok", self.message,
             self.rows_in, self.rows_out, 1 if self.flagged else 0, self.id),
        )
        self.conn.commit()
        return False
