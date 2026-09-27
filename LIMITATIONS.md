# Limitations

What is incomplete, unverified or deliberately out of scope, as of the latest commit.

## Not yet verified
- **The Linux container release gate has not been run** (EVIDENCE.md §7). The Dockerfile and `scripts/container_check.py` exist, but the Docker engine on the dev host was not running. So none of the following has been observed yet: the `run` shell adapter, an arbitrary UID, a read-only root filesystem, SQLite WAL on a `--read-only` container, POSIX directory fsync, the Python 3.12 base image, or the `--cpus 4 --memory 8g` performance numbers.
- **All measurements come from a Windows dev host** (12 CPUs, 32 GiB, Python 3.14), not the 4-core, 8 GiB Linux evaluation host.
- **The hard-kill test used Windows `TerminateProcess`,** not Linux `SIGKILL`. The script supports both, but it has only been run on Windows.

## Design limits
- **Full recomputation per snapshot.** Cost is linear in revisions ≤ B, whatever the date range. Measured: 12.2 s and 1.3 GiB at 1M keys. Past roughly 2.5–3M keys (extrapolated) the 30 s / 4 GiB targets would be missed. Moving revision selection into SQL is the planned remedy, not built.
- **Single writer.** Concurrent ingests serialise on SQLite's write lock with a 30 s busy timeout. They are out of scope and untested.
- **Quarantined rows keep no payload.** Adopting a new schema version later means re-ingesting the original input files into fresh state.
- **Orphaned staging.** Rows of a batch that was staged but never replayed stay on disk, invisible, until that batch is ingested again. Nothing cleans them up automatically.
- **No WAL checkpoint or VACUUM schedule,** and no metrics or alerting. Only stderr lines, exit codes and SQL inspection exist.
- **Batch numbers above 2^63 are not supported.** They are stored as SQLite INTEGER. Business integers (versions, amounts, FX) are stored as text and are unbounded.

## Interpretations the hidden suite could read differently
- A failpoint on an **already-committed** batch exits 0 (no-op), because 86 is specified for "a previously unseen batch".
- An erasure with an **unsupported schema version** is quarantined and does not erase.
- A **different file** claiming a committed batch number exits 1. The spec defines no code for this.
- `cutoff_batch: 0` returns an empty business report.

## Submission items still open
- `AI_LOG.md`: time and cost figures, and the tool used for the external reviews.
- The raw agent logs must be re-exported (`python3 scripts/export_ai_logs.py`) just before the final commit, because the session log keeps growing.
