"""Immutable snapshot store.

Nothing here ever overwrites a captured artefact. Files land under a
source/publication-date path, are hashed on write, and are registered in the
snapshot table with that hash. Re-running a capture for a week already held is a
no-op, not a rewrite.

This matters more than usual for the adviser feed: the upstream manifest exposes
only the current file and there is no archive to backfill from, so a week not
captured is lost permanently.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def snapshot_path(source_key: str, published_at: str, filename: str) -> Path:
    safe_date = (published_at or "undated").replace("/", "-")
    return config.SNAPSHOT_DIR / source_key / safe_date / filename


def already_held(conn: sqlite3.Connection, source_key: str, filename: str,
                 sha: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshot WHERE source_key=? AND filename=? AND sha256=?",
        (source_key, filename, sha),
    ).fetchone()


def held_for_date(conn: sqlite3.Connection, source_key: str,
                  published_at: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshot WHERE source_key=? AND published_at=?"
        " ORDER BY id DESC LIMIT 1",
        (source_key, published_at),
    ).fetchone()


def register(conn: sqlite3.Connection, source_key: str, published_at: str,
             path: Path, config_stamp: str) -> tuple[int, bool]:
    """Register a captured file. Returns (snapshot_id, was_new).

    Idempotent on (source_key, filename, sha256), so a repeated capture of an
    unchanged artefact returns the existing row rather than duplicating it.
    """
    sha = sha256_file(path)
    filename = path.name
    existing = already_held(conn, source_key, filename, sha)
    if existing is not None:
        return int(existing["id"]), False

    rel = path.relative_to(config.SNAPSHOT_DIR).as_posix()
    cur = conn.execute(
        "INSERT INTO snapshot"
        " (source_key, published_at, captured_at, filename, rel_path, bytes,"
        "  sha256, config_stamp)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (source_key, published_at,
         datetime.now(timezone.utc).isoformat(timespec="seconds"),
         filename, rel, path.stat().st_size, sha, config_stamp),
    )
    conn.commit()
    return int(cur.lastrowid), True


def resolve(conn: sqlite3.Connection, snapshot_id: int) -> Path:
    row = conn.execute("SELECT rel_path FROM snapshot WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise KeyError(f"no snapshot {snapshot_id}")
    return config.SNAPSHOT_DIR / row["rel_path"]


def latest(conn: sqlite3.Connection, source_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshot WHERE source_key=?"
        " ORDER BY published_at DESC, id DESC LIMIT 1",
        (source_key,),
    ).fetchone()


def history(conn: sqlite3.Connection, source_key: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM snapshot WHERE source_key=? ORDER BY published_at, id",
        (source_key,),
    ).fetchall()
