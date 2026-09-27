# Operations

All commands go through `./run`, or through `docker run … recon-engine` (see [README.md](README.md)). Durable state is only `--state/engine.db` plus its `-wal` and `-shm` files.

## Replay and rebuild

State is fully derived from the immutable input files. **Rebuild** means starting from an empty directory and ingesting every batch file, in any order:

```sh
for f in /inputs/batch_*.jsonl; do ./run ingest --input "$f" --state /state.new; done
```

Then switch readers to `/state.new`. Snapshots can run as soon as batches 1..B for their cutoff are present. Replaying an already-committed file is a no-op (exit 0). Rebuild is also the recovery path for corruption (`PRAGMA integrity_check` ≠ ok) and for adopting a new schema version (below).

**Copy and backup** while the engine is idle: copy all three files together. With ingests running, use `sqlite3 engine.db ".backup /backup/engine.db"`, which is online and consistent.

## Schema change

- **New input schema** (for example ledger v3): add a parser branch in `engine/records.py`, normalising to the same business tuple, and add `("ledger", 3)` to `SUPPORTED_SCHEMAS`. Until that deploys, v3 rows are quarantined as `UNSUPPORTED_SCHEMA`. Their payloads were deliberately not kept, so to bring them in, **rebuild from the input files** into fresh state. Their `row_id`s are still listed in the `unsupported` table, which scopes the work.
- **State schema:** `meta.state_schema_version` is checked by the operator. The additive DDL uses `CREATE … IF NOT EXISTS`. A breaking change is shipped as a new version plus a rebuild, never an in-place migration of money columns.

## Failed-run recovery

| Symptom | Meaning | Action |
|---|---|---|
| ingest exit 86 or killed mid-run | Batch possibly staged, never visible | Re-run the same ingest without a failpoint. Exactly one commit happens |
| ingest exit 1, "different row set" | Another file claimed a committed batch number | Stop. Compare the file against the committed `row_set_sha256`. Never force it in |
| ingest exit 1, other | Unreadable or invalid input | Nothing was published. Fix the input or environment, then re-run |
| snapshot exit 65 | Cutoff B not covered by committed 1..B | List the missing batches (query below), ingest them, retry. The previous `--out` is intact |
| snapshot exit 1 | Unexpected failure | The previous `--out` is intact. Check integrity. Rebuild if corrupt |

Crash safety relies on SQLite atomic commit with `synchronous=FULL`. It is verified by randomised hard kills: [EVIDENCE.md](EVIDENCE.md) §4.

## Deletion

- **Erasure:** ingest an `erasures` batch. From the commit onwards, every new snapshot masks the token at every cutoff. Snapshots already running keep their read view. Amounts are unchanged.
- **Names and emails are never stored.** Profiles are dropped by the parser, and payloads of unknown streams or versions are dropped too. So there is nothing to purge in state, the WAL or logs. Verify with:

  ```sh
  grep -rlaE 'SYNTHETIC|example\.invalid' /state || echo clean
  ```
- **Retained by design:** the opaque `customer_token` inside revision rows and tombstones. The spec allows it.
- **Previously published outputs** (`--out` files) are outside the engine's managed state. Regenerate them after an erasure if they must reflect it.
- **Full removal:** delete the state directory.

## Observability

- **Stderr** has one line per command, with counts only. For example: `ingest batch 3: committed 11 rows (ledger 10, unsupported 1)` or `snapshot q03: 6 settlements, 21 trace, 4 exceptions`. It never contains payload values or tokens (tested).
- **Exit codes** are the machine-readable status: 0, 65, 86, 2 or 1.
- **Audit and inspection (SQL).** Use the `sqlite3` CLI, or inside the image, which has no CLI, `python3 -c "import sqlite3; …"` against a read-only URI (`file:/state/engine.db?mode=ro`):

  ```sql
  SELECT batch, committed_at, row_count FROM batches ORDER BY batch;       -- commit log
  WITH RECURSIVE n(b) AS (SELECT 1 UNION ALL SELECT b+1 FROM n WHERE b < 12)
    SELECT b AS missing FROM n WHERE b NOT IN (SELECT batch FROM batches);   -- gaps
  SELECT batch, COUNT(*) FROM ledger_rev
    WHERE batch NOT IN (SELECT batch FROM batches) GROUP BY batch;           -- staged, uncommitted
  SELECT stream, COUNT(*) FROM unsupported GROUP BY stream;                  -- quarantine
  PRAGMA integrity_check;
  ```
- **Not built:** metrics export and alerting. The next step would be emitting ingest lag (highest committed contiguous batch versus the highest seen), snapshot latency, quarantine counts and conflict counts.

## Performance

Measured on the dev host (Windows 11, AMD Ryzen 12 cores, 32 GiB, Python 3.14, SQLite 3.50). Full tables and commands are in [EVIDENCE.md](EVIDENCE.md) §5.

| Fixture | Ingest total | Slowest snapshot | Peak RSS | State |
|---|---:|---:|---:|---:|
| 200k keys (target size), 228k rows | 3.9 s | 2.3 s | 293 MiB | 24 MiB |
| 1M keys (5×), 1.12M rows | 20.8 s | 12.2 s | 1.3 GiB | 121 MiB |

Targets: < 180 s ingest, < 30 s per snapshot, < 4 GiB, < 5 GiB. Snapshot cost is linear in revisions ≤ B, whatever the date range. The projected ceiling is about 2.5–3M keys (DESIGN.md, *Limits*). Linux numbers under `--cpus 4 --memory 8g` come from `scripts/container_check.py` (see EVIDENCE.md for status).

## Incident runbook: settlement snapshot fails after a host restart

**Trigger:** the daily job `./run snapshot --query close.json --state /state --out /reports/close.json` exits **65** at 06:00. The host was rebooted at 02:10 while batch 41 was being ingested. `/reports/close.json` still holds yesterday's report.

1. **Contain.** Don't delete or edit `/state`, and don't hand-edit the report. The previous report is intact by design (atomic publish). Tell consumers the close is delayed.
2. **Confirm state health:**

   ```sh
   sqlite3 /state/engine.db "PRAGMA integrity_check;"
   ```

   Expect `ok`. If it isn't, go to step 6.
3. **Find the gap and the stranded staging:** run the "gaps" and "staged, uncommitted" queries above. The expected result is that batch 41 is missing from `batches`, with rows present in `ledger_rev` for batch 41. The ingest was killed after staging and before its commit.
4. **Replay** the exact file:

   ```sh
   ./run ingest --input /inputs/batch_41.jsonl --state /state
   ```

   Expect exit 0 and `committed … rows`. If it exits 1 with "different row set", someone has the wrong file. Escalate to the data owner and don't proceed.
5. **Re-run the snapshot** and check it:

   ```sh
   ./run snapshot --query close.json --state /state --out /reports/close.json
   python3 tools/check_output.py /reports/close.json
   ```

   Compare totals with yesterday's report for sanity (settled dates should only change through known corrections), then release.
6. **If the state is corrupt:** rebuild into `/state.new` from `/inputs` (see *Replay and rebuild*), verify step 5 against it, then swap directories. Expected time is proportional to input size: 3.9 s at the 200k-key scale on the dev host.
7. **Afterwards:** record the timeline, and alert on "staged, uncommitted" rows older than one ingest interval so this is caught before the close.
