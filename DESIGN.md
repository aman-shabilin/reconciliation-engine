# Design

A local bitemporal settlement engine: Python 3 standard library and SQLite (WAL mode). Append-only revisions carry their knowledge time (batch number). Every snapshot recomputes the as-of answer from those revisions inside one read transaction. Measured headroom at the published scale is about 60× (see [EVIDENCE.md](EVIDENCE.md)), so full recomputation was chosen over incremental caches. The caches were the main source of correctness risk.

## Data model

One SQLite file, `state/engine.db` ([engine/store.py](engine/store.py)):

| Table | Holds | Key |
|---|---|---|
| `batches` | commit marker and checkpoint: `committed_at`, `row_count`, `row_set_sha256` | `batch` |
| `ledger_rev`, `merchant_rev`, `fx_rev` | every supported revision, with typed columns for exactly the declared business fields, plus `batch` | `row_id` |
| `unsupported` | `row_id`, `batch`, `stream` only | `row_id` |
| `erasures` | `customer_token` tombstones, plus `batch` | `row_id` |
| `meta` | state schema version | `key` |

There is **no payload or JSON column anywhere**. Profiles are dropped by the parser ([engine/records.py](engine/records.py)) before any storage code sees them. Unsupported rows lose their payload, which may contain PII, at parse time. Versions, amounts and FX fractions are stored as decimal TEXT, because SQLite INTEGER is 64-bit and amounts are unbounded. All arithmetic uses Python `int`.

Each stream parses into a NamedTuple of only its declared fields, with ledger v2 `amount_minor_text` already converted to int. **Tuple equality is business equality.** Envelope fields and extra fields cannot influence duplicate or conflict detection, and v1/v2 equivalents compare equal.

## Invariants

1. A batch is visible iff its row exists in `batches`.
2. A snapshot at cutoff B runs only if batches 1..B are all committed. Otherwise it exits 65 and `--out` is not touched. Given (1), every row with `batch <= B` is committed, so business reads filter on `batch` alone. **The gap check is therefore load-bearing** and is commented as such in the code.
3. Erasures ignore B, so they are always read joined to `batches`.
4. Stored revision rows for a committed batch are exactly the rows of the file that committed it. Re-staging discards the batch's uncommitted rows first.
5. Names and emails never reach SQLite, stderr or temp files.
6. Results at cutoff B depend only on committed batches ≤ B plus the set of committed erasures.

## State transitions and transaction boundary

Per batch: **absent → staged → committed**.

- **Transaction A (stage):** `BEGIN IMMEDIATE`, re-check that the batch is not committed, delete its uncommitted rows, `INSERT OR IGNORE` the parsed rows into the typed tables, `COMMIT` (durable, `synchronous=FULL`). `--failpoint after-stage` calls `os._exit(86)` here. The rows are durable but invisible under invariants 1–3.
- **Transaction B (publish):** `BEGIN IMMEDIATE`, insert the `batches` row. `--failpoint before-commit` calls `os._exit(86)` immediately before `COMMIT`, so the open transaction is discarded exactly as a crash would discard it. This one-row commit is the **atomic visibility boundary** for the batch payload, revision history, erasure effects and checkpoint together.

The two failpoints are distinct: one follows a durable commit, the other falls inside the publishing transaction. Replay restarts from parsing. If the batch is already committed with the same `row_id` set, the command is a no-op and exits 0, even when a failpoint is given, because the batch is not "previously unseen". A *different* `row_id` set claiming a committed batch number is an operator error: it exits 1 and changes nothing. The fingerprint is over sorted `row_id`s, not raw bytes. A raw-bytes hash would retain a derivative of profile PII and would reject a retry whose lines were reordered.

Readers: a snapshot opens the database read-only and does all reads, including the gap check and erasures, inside one deferred transaction. WAL gives that transaction a stable view from its first read, so concurrent commits, erasures included, are invisible to it. This is exactly the "read transaction begins" semantics in the spec. Output is written to a temp file in the target directory, fsynced, then published with `os.replace`, followed by a directory fsync on POSIX.

## Time semantics

- **Knowledge time** = batch number. A query sees supported rows with `batch <= cutoff_batch`. Arrival order is irrelevant: selection is by business `version`.
- **Effective time** = `effective_at` (UTC). The settlement date and the FX key are `effective_at[:10]`. Merchant intervals are half-open, `valid_from <= t < valid_to`, compared as fixed-format strings.
- **Erasure** is the deliberate exception: masking uses every erasure committed before the snapshot's read transaction began, at any cutoff. Amounts never change.

## Selection, joins and money

[engine/core.py](engine/core.py) is pure (no I/O):

1. For each key, take the highest version. Identical payloads collapse. Distinct payloads at the top version block the key and emit one `REVISION_CONFLICT`. There is no fallback to lower versions and no tie-break by arrival.
2. Blocked merchant and FX keys are absent from joins.
3. Each selected in-range POST produces only its first join error, in the order MISSING_MERCHANT, then AMBIGUOUS_MERCHANT, then MISSING_FX.
4. `usd = round_half_even(amount × num / den)` in integer arithmetic (`divmod`, symmetric for negatives), per entry, then summed.

`evaluate()` applies the same functions to parsed rows in memory. It is the **oracle** for storage tests and for scale verification.

## Lineage

Each trace row identifies `(entry_id, version)`. In state, that pair resolves to the `row_id` and `batch` that delivered it. `batches` records when each batch became visible, its row count and fingerprint. Exceptions name the stream and key: `row_id` for unsupported rows, the business key otherwise. Only erased customer tokens are hidden, and non-PII lineage is unchanged, as required.

## Interpretations

- An unsupported-schema erasure is quarantined and does not erase ("quarantined … never take part").
- `cutoff_batch = 0` returns an empty business report (nothing needs to be committed).
- Exit 2 is a usage error, and exit 1 is rejected input or an unexpected failure. Messages carry only exception types and batch numbers, never values, and there is no debug mode that relaxes this.

## Alternatives considered

- **Materialised per-(date, merchant) aggregates updated per batch.** They are faster for current-cutoff reads but need per-cutoff versions and invalidation of both old and new dates when revisions move entries. Late merchant and FX corrections also rewrite many groups. Rejected: the measured recompute cost does not justify the risk.
- **Revision selection in SQL** (`GROUP BY key`, `COUNT(DISTINCT …)` at the max version). This is the first optimisation if scale grows, because only in-range rows would reach Python. Deferred to keep one code path shared with the oracle.
- **A separate staging table.** An earlier design held canonical JSON there and was rejected: unknown payloads could have lingered in free pages and the WAL. Direct staging with a commit marker needs no copy step.
- **Key destruction for PII.** Unnecessary, because PII is never stored.
- **DuckDB or Parquet.** Faster scans, but it adds a pinned dependency and a weaker story for transactional single-file durability.

## Limits

- Every snapshot loads all revisions up to B. Measured on the dev host: 200k keys take ≤ 2.3 s and 293 MiB; 1M keys take ≤ 12.2 s and 1.3 GiB. The empty-range query still costs 4.4 s at 1M keys, because conflicts must be reported for all keys. Extrapolating linearly, the 30 s / 4 GiB targets are exceeded at roughly 2.5–3M keys.
- Single writer. Concurrent ingests serialise on SQLite's write lock but are not otherwise designed for (out of scope).
- Rows from a staged but never-replayed batch stay on disk, invisible, until that batch is ingested again.
- Payloads of quarantined unsupported rows are gone, so supporting a new schema later requires re-ingesting the original files into fresh state.

## Scale note: one billion events per day

**Assumptions.** 1B events/day ≈ 11.6k/s average and ~60k/s peak. Events are ~300 B, so ~300 GB/day raw and ~60 GB/day as Parquet. There are 30 days of late arrivals, 15-minute correction visibility, 99.9% query availability and a daily close. Merchants and FX stay small (≤ 10⁶ rows).

**Ingest and knowledge time.** Producers write to a log (Kafka, 256 partitions keyed by `entry_id`). A stream job (Flink or Spark Structured Streaming) commits a micro-batch every 5 minutes into Apache Iceberg tables. **One Iceberg commit is the visibility boundary**: it contains the revisions, the erasure tombstones and the source offsets, replacing `batches`. The Iceberg snapshot sequence number replaces `batch`. An as-of query is time travel to the latest snapshot at or before the cutoff. The gap check becomes "all offsets up to the watermark committed".

**Partitioning and skew.** Revisions are partitioned by effective date (day) and `bucket(64, entry_id)`. Revision selection is keyed by `entry_id`, so a hot merchant cannot skew it. Aggregation by (date, merchant) salts the hottest merchants into N sub-keys and merges them afterwards. Merchant and FX dimensions are broadcast.

**Incremental maintenance.** A stateful operator (RocksDB state, TTL = late window) keeps the selected revision per `entry_id` and emits a *retract and insert* pair for the old and new contribution, so date and merchant moves are exact. An FX or merchant correction marks the affected (date, currency) or (merchant, interval) partitions, which are recomputed from the revision table. The 30-day window bounds that work.

**Compaction.** Hourly Iceberg file compaction, keeping every revision (as-of needs them). Snapshots expire after the audit retention period. As-of queries older than that are refused, not approximated.

**Consistency and availability.** The daily close pins an Iceberg snapshot ID as the official close. Later corrections are reported as adjustments against it. Readers query per-(knowledge snapshot, date) aggregate tables through a replicated engine (two or more Trino coordinators across zones), with snapshot isolation provided by the table format.

**Privacy.** Profiles are dropped at the ingestion edge. Masking is a read-time join against a small replicated erasure table, so immutable files need no rewrite. The token tombstone is permitted by the spec.

**Costs (resource units).** The stream job is ~2k events/s per vCPU for parse, select and emit, so 60k/s peak needs ~30 vCPU, or 48 with headroom. RocksDB state: ~30B keys in the window × ~100 B ≈ 3 TB of SSD across ~16 nodes. Storage: ~1.8 TB hot Parquet per 30 days, plus history. Query tier: 8–16 vCPU per replica for aggregate reads.

**Deployment and backfill cutover.** Build the new tables from history (a batch job over the Iceberg revision table plus the Kafka retention tail). Run in shadow for ≥ 7 days, diffing per (date, merchant) against the current system. Switch readers by repointing views, and keep the old path for rollback until one daily close matches.

**What changes from the local solution:** SQLite becomes a log plus a table format, the batch number becomes a snapshot sequence, full recomputation becomes retract/insert deltas plus bounded partition recompute, and one Python process becomes a partitioned stream job. The invariants carry over unchanged: one atomic visibility boundary, the gap check, business-version selection, per-entry rounding, and masking at read time. *None of this was implemented.*
