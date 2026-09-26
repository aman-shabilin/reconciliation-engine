"""Durable state in SQLite: two-transaction ingest and snapshot-isolated reads.

Visibility model
----------------
Rows are staged directly into typed per-stream tables, tagged with their
batch number. A batch becomes visible only when its row is committed into
`batches`; that single-row commit is the atomic visibility boundary for the
batch payload, revision history, erasure effects and the checkpoint.

Staged-but-uncommitted rows are invisible because:
  * business rows are read with `batch <= B`, and a snapshot only runs after
    `require_contiguous` has proven batches 1..B are all committed, so every
    row it reads belongs to a committed batch;
  * erasures ignore B, so they are always read joined to `batches`.

There is no generic payload column anywhere: profile names/emails and
unknown-schema payloads are never written (see engine.records).

All business integers (versions, amounts, FX fractions) are stored as
decimal TEXT because SQLite INTEGER is 64-bit and amounts are unbounded.
"""
import hashlib
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from .core import Query, build_report
from .records import (Erasure, FxRev, LedgerRev, MerchantRev, Unsupported,
                      parse_line)

DB_NAME = "engine.db"
STATE_SCHEMA_VERSION = "1"
FAILPOINTS = ("after-stage", "before-commit")

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    batch        INTEGER PRIMARY KEY,
    committed_at TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    row_set_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger_rev (
    row_id         TEXT PRIMARY KEY,
    batch          INTEGER NOT NULL,
    entry_id       TEXT NOT NULL,
    version        TEXT NOT NULL,
    op             TEXT NOT NULL,
    merchant_id    TEXT NOT NULL,
    customer_token TEXT NOT NULL,
    effective_at   TEXT NOT NULL,
    currency       TEXT NOT NULL,
    amount_minor   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_rev_batch ON ledger_rev (batch);
CREATE TABLE IF NOT EXISTS merchant_rev (
    row_id      TEXT PRIMARY KEY,
    batch       INTEGER NOT NULL,
    merchant_id TEXT NOT NULL,
    valid_from  TEXT NOT NULL,
    version     TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    region      TEXT NOT NULL,
    tier        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fx_rev (
    row_id        TEXT PRIMARY KEY,
    batch         INTEGER NOT NULL,
    currency      TEXT NOT NULL,
    date          TEXT NOT NULL,
    version       TEXT NOT NULL,
    usd_micro_num TEXT NOT NULL,
    usd_micro_den TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unsupported (
    row_id TEXT PRIMARY KEY,
    batch  INTEGER NOT NULL,
    stream TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS erasures (
    row_id         TEXT PRIMARY KEY,
    batch          INTEGER NOT NULL,
    customer_token TEXT NOT NULL
);
"""


class GapError(Exception):
    """Snapshot cutoff B requires batches 1..B, and at least one is missing."""


class InputError(Exception):
    """The input file violates the transport contract."""


class IntegrityError(Exception):
    """A different file claims the number of an already-committed batch."""


def _hard_exit(code: int) -> None:
    # Crash-equivalent: no Python cleanup, no rollback call, no commit.
    os._exit(code)


def _connect(db_path: Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True,
                               isolation_level=None)
    else:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")    # fsync the WAL on every commit
    conn.execute("PRAGMA temp_store=MEMORY")   # no SQLite temp files outside state
    conn.execute("PRAGMA secure_delete=ON")    # defence in depth; we never write PII
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def row_set_fingerprint(rows) -> str:
    """SHA-256 over the batch's sorted distinct row_ids.

    Identifies a batch's content without deriving anything from payloads:
    row_ids are opaque and a reused row_id with different bytes is out of
    scope, so the row_id set determines the batch. Unlike a hash of the raw
    file it is not a fingerprint of PII, and it is stable under line
    reordering or whitespace changes in a retried file.
    """
    joined = "\n".join(sorted({r.row_id for r in rows}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def read_batch_file(input_path: Path):
    """Parse one JSONL batch file. Returns (batch, rows, fingerprint, line_count)."""
    raw = input_path.read_bytes()
    rows = [parse_line(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not rows:
        raise InputError("batch file is empty")
    batches = {r.batch for r in rows}
    if len(batches) != 1:
        raise InputError("lines in one file carry different batch numbers")
    batch = rows[0].batch
    if type(batch) is not int or batch < 1:
        raise InputError("batch must be a positive integer")
    return batch, rows, row_set_fingerprint(rows), len(rows)


STAGED_TABLES = ("ledger_rev", "merchant_rev", "fx_rev", "unsupported", "erasures")


def _discard_uncommitted(conn: sqlite3.Connection, batch: int) -> None:
    """Drop rows left by an earlier, never-committed staging of this batch.

    They are invisible, but without this a different file later committed
    under the same batch number would publish them too. Afterwards the
    staged rows are exactly the current file's rows.
    """
    for table in STAGED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE batch = ?", (batch,))


def _stage(conn: sqlite3.Connection, rows) -> Counter:
    ledger, merchants, fx, unsupported, erasures = [], [], [], [], []
    counts = Counter()
    for row in rows:
        r = row.record
        if isinstance(r, LedgerRev):
            ledger.append((row.row_id, row.batch, r.entry_id, str(r.version), r.op,
                           r.merchant_id, r.customer_token, r.effective_at,
                           r.currency, str(r.amount_minor)))
            counts["ledger"] += 1
        elif isinstance(r, MerchantRev):
            merchants.append((row.row_id, row.batch, r.merchant_id, r.valid_from,
                              str(r.version), r.valid_to, r.region, r.tier))
            counts["merchants"] += 1
        elif isinstance(r, FxRev):
            fx.append((row.row_id, row.batch, r.currency, r.date, str(r.version),
                       str(r.usd_micro_num), str(r.usd_micro_den)))
            counts["fx"] += 1
        elif isinstance(r, Unsupported):
            unsupported.append((row.row_id, row.batch, r.stream))
            counts["unsupported"] += 1
        elif isinstance(r, Erasure):
            erasures.append((row.row_id, row.batch, r.customer_token))
            counts["erasures"] += 1
        else:  # profile: discarded at parse time, nothing to write
            counts["profiles_discarded"] += 1
    # INSERT OR IGNORE on row_id: an exact row redelivered within the file
    # collapses to one.
    conn.executemany("INSERT OR IGNORE INTO ledger_rev VALUES (?,?,?,?,?,?,?,?,?,?)", ledger)
    conn.executemany("INSERT OR IGNORE INTO merchant_rev VALUES (?,?,?,?,?,?,?,?)", merchants)
    conn.executemany("INSERT OR IGNORE INTO fx_rev VALUES (?,?,?,?,?,?,?)", fx)
    conn.executemany("INSERT OR IGNORE INTO unsupported VALUES (?,?,?)", unsupported)
    conn.executemany("INSERT OR IGNORE INTO erasures VALUES (?,?,?)", erasures)
    return counts


def _ensure_schema(conn: sqlite3.Connection) -> None:
    # Statement by statement: executescript() would implicitly COMMIT.
    conn.execute("BEGIN IMMEDIATE")
    for statement in _DDL.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('state_schema_version', ?)",
                 (STATE_SCHEMA_VERSION,))
    conn.execute("COMMIT")


def _is_committed(conn: sqlite3.Connection, batch: int) -> Optional[str]:
    row = conn.execute("SELECT row_set_sha256 FROM batches WHERE batch = ?", (batch,)).fetchone()
    return row[0] if row else None


def ingest(input_path: Path, state_dir: Path, failpoint: Optional[str] = None,
           crash: Callable[[int], None] = _hard_exit, log=print) -> str:
    """Ingest one batch file. Returns 'committed' or 'already-committed'.

    Raises IntegrityError, changing nothing, if the batch is already
    committed with a different row set: that is an operator mistake, not a
    retry, and silently accepting it would hide it.

    Transaction A durably stages rows (failpoint after-stage fires after it
    commits). Transaction B inserts the checkpoint row (failpoint
    before-commit fires inside it, immediately before COMMIT).
    """
    if failpoint is not None and failpoint not in FAILPOINTS:
        raise ValueError(f"unknown failpoint {failpoint!r}")
    batch, rows, fingerprint, line_count = read_batch_file(input_path)

    state_dir.mkdir(parents=True, exist_ok=True)
    conn = _connect(state_dir / DB_NAME)
    try:
        _ensure_schema(conn)
        prior = _is_committed(conn, batch)
        if prior is not None:
            if prior != fingerprint:
                raise IntegrityError(f"batch {batch} is already committed with a different "
                                     f"row set; refusing to ingest this file")
            log(f"ingest batch {batch}: already committed; no-op")
            return "already-committed"

        # Transaction A: durable staging, invisible until the checkpoint commits.
        conn.execute("BEGIN IMMEDIATE")
        if _is_committed(conn, batch) is not None:
            # Committed between our check and this lock: never touch its rows.
            conn.execute("ROLLBACK")
            log(f"ingest batch {batch}: already committed; no-op")
            return "already-committed"
        _discard_uncommitted(conn, batch)
        counts = _stage(conn, rows)
        conn.execute("COMMIT")
        if failpoint == "after-stage":
            log(f"ingest batch {batch}: failpoint after-stage")
            crash(86)
            return "failpoint"  # only reached when crash() is a test double

        # Transaction B: the atomic visibility boundary.
        conn.execute("BEGIN IMMEDIATE")
        if _is_committed(conn, batch) is None:
            conn.execute("INSERT INTO batches VALUES (?, strftime('%Y-%m-%dT%H:%M:%SZ','now'), ?, ?)",
                         (batch, line_count, fingerprint))
        if failpoint == "before-commit":
            log(f"ingest batch {batch}: failpoint before-commit")
            crash(86)
            conn.execute("ROLLBACK")
            return "failpoint"
        conn.execute("COMMIT")
        summary = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        log(f"ingest batch {batch}: committed {line_count} rows ({summary})")
        return "committed"
    finally:
        conn.close()


def require_contiguous(conn: sqlite3.Connection, cutoff: int) -> None:
    """Fail unless every batch 1..cutoff is committed.

    Load-bearing for correctness, not just input validation: once this
    passes, every row with batch <= cutoff belongs to a committed batch, so
    business reads may filter on batch alone. Do not remove or weaken it
    without joining every read to `batches`.
    """
    (present,) = conn.execute(
        "SELECT COUNT(*) FROM batches WHERE batch BETWEEN 1 AND ?", (cutoff,)).fetchone()
    if present != max(cutoff, 0):
        raise GapError(f"cutoff {cutoff} requires batches 1..{cutoff}; {present} committed")


def load_report(state_dir: Path, query: Query,
                _after_gap_check: Callable[[], None] = lambda: None) -> dict:
    """Compute a report inside one read transaction.

    WAL mode gives the transaction a stable snapshot from its first read, so
    concurrent commits (including erasures) are invisible to it.
    """
    db_path = state_dir / DB_NAME
    if not db_path.exists():
        if query.cutoff_batch >= 1:
            raise GapError(f"cutoff {query.cutoff_batch} requires batches; state is empty")
        return build_report(query, [], [], [], [], [])

    conn = _connect(db_path, readonly=True)
    try:
        conn.execute("BEGIN")
        require_contiguous(conn, query.cutoff_batch)
        _after_gap_check()  # test hook: simulate a concurrent writer here
        b = (query.cutoff_batch,)
        ledger = [LedgerRev(e, int(v), op, m, c, t, cur, int(a)) for e, v, op, m, c, t, cur, a in
                  conn.execute("SELECT entry_id, version, op, merchant_id, customer_token, "
                               "effective_at, currency, amount_minor FROM ledger_rev "
                               "WHERE batch <= ?", b)]
        merchants = [MerchantRev(m, vf, int(v), vt, reg, tier) for m, vf, v, vt, reg, tier in
                     conn.execute("SELECT merchant_id, valid_from, version, valid_to, region, "
                                  "tier FROM merchant_rev WHERE batch <= ?", b)]
        fx = [FxRev(c, d, int(v), int(n), int(den)) for c, d, v, n, den in
              conn.execute("SELECT currency, date, version, usd_micro_num, usd_micro_den "
                           "FROM fx_rev WHERE batch <= ?", b)]
        unsupported = [Unsupported(r, s) for r, s in
                       conn.execute("SELECT row_id, stream FROM unsupported WHERE batch <= ?", b)]
        # Erasures apply regardless of cutoff, but only once committed.
        erased = [t for (t,) in conn.execute(
            "SELECT DISTINCT e.customer_token FROM erasures e "
            "JOIN batches USING (batch)")]
        conn.execute("COMMIT")
    finally:
        conn.close()
    return build_report(query, ledger, merchants, fx, unsupported, erased)


def write_atomic(path: Path, data: bytes) -> None:
    """Publish `data` at `path` atomically; on failure the old file is untouched."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    if hasattr(os, "O_DIRECTORY"):  # POSIX: make the rename itself durable
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
