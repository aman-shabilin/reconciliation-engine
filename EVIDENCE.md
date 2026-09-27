# Evidence

Every result below was **measured** by running the command shown. The raw output is in [evidence/](evidence/), and each file starts with its command and a UTC timestamp. Anything not yet measured is labelled **NOT MEASURED** or **PROPOSED**.

**Dev host:** Windows 11 Pro 10.0.26200, AMD Ryzen (family 25, model 33), 12 logical CPUs, 31.9 GiB RAM, Python 3.14.6, SQLite 3.50.4, Git Bash. **This is not the evaluation host** (Linux, 4 cores, 8 GiB). §1–6 were measured on it. **§7 repeats the key checks in the Linux container** under the evaluation constraints, including the 4-CPU and 8 GiB limits.

## 1. Correctness: public fixtures through `./run`

[evidence/public-cli-host.txt](evidence/public-cli-host.txt): ingest batches 01..12 into fresh state, one process each, then run all 10 queries.

```
$ ./run ingest --input $PWD/data/batch_NN.jsonl --state $STATE       # x12, all exit 0
$ ./run snapshot --query $PWD/queries/<q>.json --state $STATE --out $OUT/<q>.json
```

Observed: **all 10 outputs (day2, empty, q01–q05, q07, q09, q12) JSON-equal `expected/`**, and `tools/check_output.py` passes. Test `tests/test_public_fixtures.py` shows the core gives the same results under 10 random row orders and with every row duplicated.

## 2. Tests and mutation sensitivity

```
$ python -m unittest discover -s tests -t . -v      # evidence/tests-host.txt
Ran 80 tests in 29.292s
OK (skipped=1)
```

The skipped test executes the POSIX `run` shell adapter. It needs Linux, so it runs in the container (§7).

What the suites cover:
- **`test_money`:** worked example 1, negative ties, 2^53+1, 5,000-digit integers, and 25,000 random cases checked against `round(Fraction)`.
- **`test_records`:** v1/v2 equality, extra fields ignored, strict amount text, schema routing, profiles dropped.
- **`test_core`:** worked examples 1–7 plus 30 rule cases.
- **`test_durability`:** everything in §3, §6 and the batch-identity rules.

```
$ python scripts/mutants.py                          # evidence/mutants-host.txt
baseline passes
... 28 lines "killed <mutant> by <test id>"
28/28 mutants killed
```

Each mutant is a plausible spec violation, for example:
- half-up rounding
- float parsing
- an inclusive `valid_to`
- the last arriving row winning
- erasure bounded by the cutoff
- gap check disabled
- commit placed before the `before-commit` failpoint
- a fake early `after-stage` exit
- stale staged rows published
- a different file accepted for a committed batch

The unmutated baseline must pass first, and each kill names the test that caught it.

## 3. Replay, recovery and incremental state

From `tests/test_durability.py` (in [evidence/tests-host.txt](evidence/tests-host.txt)):

| Check | Result |
|---|---|
| `--failpoint after-stage` / `before-commit` on batch 10 (erasures) → 86 | pass. Snapshots with B ≤ 9 are byte-identical to before; B ≥ 10 exits 65; the staged erasure does **not** mask |
| Replay without failpoint → commits once; second replay and failpoint on a committed batch → exit 0, row counts unchanged | pass |
| Real subprocess `os._exit(86)` for both failpoints, then `snapshot` 65 → replay → correct | pass |
| `after-stage` really staged: ledger rows are durable while `batches` is unchanged (guards against a fake early exit) | pass |
| 20 random ingest orders plus 4 whole-file retries each; after **every** ingest, every query either exits 65 (1..B incomplete) or equals the in-memory oracle over the committed batches; final outputs equal `expected/` | pass |
| Gap 1, 2, 4 → q03/q04/q12 exit 65, q02 correct | pass |
| Reader isolation: batch 10 (erasures) commits *between* a snapshot's gap check and its reads → that snapshot stays unmasked; the next one is masked | pass |
| Different file for a committed batch → exit 1, state unchanged; same batch with lines reordered → no-op | pass |
| Orphan rows from a different, abandoned staging are never published | pass |

Gap with a pre-existing output ([evidence/public-cli-host.txt](evidence/public-cli-host.txt)): the `--out` sha256 was `6435…4978` before and `6435…4978` after, with exit 65.

## 4. Crash recovery under hard kill

```
$ python scripts/crash_test.py --trials 40 --seed 7   # evidence/crash-host.txt
40 trials, no violations. outcomes: {'killed before staging': 35, 'killed after commit': 2,
                                     'finished': 2, 'killed after staging': 1}
```

Setup: 200k-key scale fixture, batches 1..10 committed. Each trial ingests batch 11 or 12 (batch 12 carries erasures) and terminates it hard (TerminateProcess on Windows) after a random delay. Each trial then checks:
1. `PRAGMA integrity_check` returns ok.
2. The previous-cutoff snapshot is byte-identical to the matching reference.
3. The new cutoff is either the full reference or exit 65.
4. Replay exits 0, and the new-cutoff snapshot is byte-identical to the reference.

The window inside the final one-row commit is too narrow to hit by timing. It is covered deterministically by the `before-commit` failpoint in §3. The staging-to-commit window is targeted directly on Linux in §7 (30/30 kills landed there).

## 5. Scale and performance

Generated by `python scripts/gen_scale.py` (seed 42). Every edge-case family is included, plus skew: 40% of entries on one merchant, 25% on one day, and one merchant with 720 intervals.

**200k keys, 228,462 rows, 55 MiB** ([evidence/bench-host-200k.txt](evidence/bench-host-200k.txt)):

```
$ python scripts/bench.py --verify
```

| Measure | Value | Target |
|---|---:|---:|
| Ingest, 12 processes total | **3.9 s** (max 0.52 s per batch) | < 180 s |
| Peak RSS, ingest | 50 MiB | < 4 GiB |
| Snapshot: full / hot_day / early_cutoff / empty | **2.27 / 1.14 / 1.13 / 0.80 s** | < 30 s |
| Peak RSS, snapshot | 293 MiB (full) | < 4 GiB |
| State on disk | 24.4 MiB | < 5 GiB |
| Snapshot equals in-memory oracle | yes, all 4 queries | — |

The `full` query contains 194,905 trace rows and 1,214 exceptions, covering every code, and 193 USD amounts beyond 2^53.

**1M keys, 1,118,269 rows (5× the target)** ([evidence/bench-host-1m.txt](evidence/bench-host-1m.txt)): ingest 20.8 s total, snapshots 4.4–12.2 s, peak RSS 1.3 GiB, state 121 MiB.

The cost is linear in revisions ≤ B. The ceiling projected from these runs (≈ 2.5–3M keys) is **extrapolated, not measured**.

Peak RSS is measured per engine process (`PeakWorkingSetSize` on Windows, `wait4` `ru_maxrss` on Linux). Wall times include process start-up.

## 6. Privacy

| Check | Command or test | Result |
|---|---|---|
| No names or emails in state after all public batches, including the batch-11 profile replayed after its erasure | `grep -rlaE 'SYNTHETIC\|example\.invalid' $STATE` ([evidence/public-cli-host.txt](evidence/public-cli-host.txt)); `PrivacyTest` | none in `engine.db`, `-wal` or `-shm` |
| Unknown stream or version carrying PII in its payload is not persisted | `test_unknown_schema_payload_never_persisted` | none, and the exceptions still list the rows |
| Stderr contains no PII or tokens | `test_stderr_has_no_pii_or_tokens` | pass |
| Masking at every cutoff once the erasure has committed (q01–q09 included) | public parity (§1); `test_6_…`; `test_erasure_masks_token_only` | pass |
| Uncommitted erasure does not mask; concurrent reader keeps its view | §3 | pass |
| Checkpoint holds no payload-derived hash | `test_checkpoint_holds_no_payload_derived_fingerprint` | pass |

## 7. Linux container (release gate)

```
$ python scripts/container_check.py
```

Raw output: [evidence/container-check.txt](evidence/container-check.txt) (2026-09-27, `ALL CONTAINER CHECKS PASSED`, exit 0).

**Environment:**
- Docker 29.8.0 (Docker Desktop, WSL 2 kernel 6.18, x86_64) on the dev host.
- Image `python:3.12-slim@sha256:f77ac9e4…`, which provides **Python 3.12.14** and **SQLite 3.46.1**.
- Every engine command ran in its **own container** with `--read-only --user 23456:23456 --network none --tmpfs /tmp`. That UID is not the image's default user.
- State lived in a named volume that persisted across containers. Inputs were bind-mounted read-only.

| Check | Observed |
|---|---|
| Self-test suite in the image (`selftest`) | `Ran 80 tests in 10.246s … OK`, **0 skipped**: the POSIX `run` adapter test executed |
| `/app` immutable for the runtime user | `touch /app/x` exits 1; `/app/run` is executable and the sources are readable |
| Public fixtures, 12 ingests and 10 snapshots | **all 10 match `expected/`** |
| PII scan of the state volume | no match in `engine.db`, `-wal` or `-shm`; files owned by UID 23456 |
| `--failpoint after-stage` / `before-commit` on batch 3 | exit **86**; q03 exits **65** with the pre-existing `--out` unchanged; q02 equals the oracle; replay exits 0; second replay and failpoint-on-committed are no-ops; q03 equals the oracle |
| Gap: batches 1, 2, 4, then q04 | exit **65**; 4 KiB random `--out` sha256 `453f1c08…` unchanged |

**Scale at the evaluation limits** (`--cpus 4 --memory 8g --memory-swap 8g`, 200k-key fixture, `bench.py --verify`). `os.cpu_count()` and `/proc/meminfo` inside the container report the VM's 12 CPUs and 15.6 GiB. The 4-CPU and 8 GiB limits are enforced by cgroups. The engine is single-threaded.

| Measure | Linux container | Target |
|---|---:|---:|
| Ingest total (12 containers) | **4.1 s** (max 0.43 s per batch) | < 180 s |
| Snapshot full / hot_day / early_cutoff / empty | **3.08 / 1.44 / 1.56 / 1.02 s** | < 30 s |
| Peak RSS | 51 MiB ingest, **439 MiB** snapshot | < 4 GiB |
| State | 24.4 MiB | < 5 GiB |
| Equal to in-memory oracle | yes, all 4 queries | — |

**Hard kill with SIGKILL on Linux:**
- **Timed kills** (`crash_test.py --trials 30 --seed 7`, in the container check): 30 trials, no violations. On Linux almost every timed kill lands during parsing (28 before staging, 2 after finishing). A second timed run concentrated late in the ingest (`--window 0.55 1.05`) still hit parsing in 28 of 30 trials, because staging and commit take only milliseconds on this filesystem.
- **So the kill is triggered by state instead** (`--kill-when-staged`, [evidence/crash-linux-staged.txt](evidence/crash-linux-staged.txt)). The test polls the state read-only and sends SIGKILL the moment the batch's staged rows are visible. Result: **30/30 killed after staging and before commit**, including 15 on batch 12 with its erasures staged. There were **no violations**: the database was intact, the batch was invisible, the previous cutoff was byte-identical, the staged erasures masked nothing, and replay reached the reference.

## 8. Proposed, not measured

- The one-billion-events-per-day design (DESIGN.md, *Scale note*), including all resource estimates.
- The ≈ 2.5–3M-key ceiling for full recomputation (an extrapolation from §5).
- Ingest-lag and latency metrics (OPERATIONS.md, *Observability*).
