# Working plan: The Reconciliation Engine

This is a working document for me. It is not one of the required deliverables, but it is fine to commit it.

## 1. What the assignment is really asking

Build a small **bitemporal settlement engine** that runs locally. It ingests JSONL batches into a durable state directory and answers "as of knowledge cutoff B, for effective dates [start, end)" queries with exact output.

The assignment tests four things:
1. **Exact correctness**: integer money, half-even rounding per entry, revision selection and conflict detection.
2. **Time semantics**: *knowledge time* (`cutoff_batch`) is separate from *effective time* (`effective_at` and the date range). Later data must never change the answer at an earlier cutoff.
3. **Durability**: atomic batch commits, truthful failpoints, idempotent retries, rejection of gaps, and readers that only ever see committed state.
4. **Privacy**: erasure applies to all snapshots, including historical ones, and names/emails can never be recovered from managed state.

Most of the score comes from hard gates. **One wrong hidden result can fail the whole submission.** Correctness matters more than performance, and performance matters more than prose.

## 2. Every requirement, as a checklist

### 2.1 Input and types (SPECIFICATION §2)
- [ ] Input is UTF-8 JSONL. One file is one batch, and every line in it has the same `batch`. Batches are never empty.
- [ ] Envelope fields: `batch`, `row_id` (globally unique), `stream`, `schema_version`, `payload`. **Ignore extra envelope and payload fields** when checking business equality.
- [ ] Batches can arrive in any order. Whole files can be retried. A row can be redelivered byte-for-byte (dedupe by `row_id`).
- [ ] Supported schemas: `ledger` v1 and v2, `merchants` v1, `fx` v1, `profiles` v1, `erasures` v1. **Any other stream or version** becomes `UNSUPPORTED_SCHEMA` and never takes part in revision selection. The fixtures include `ledger` v99 and `future_stream`.
- [ ] Persist unsupported rows as `(batch, row_id, stream)` only. **Never write their payload anywhere, not even temporarily**, because an unknown payload may contain PII.
- [ ] Ledger v2 `amount_minor_text` is parsed exactly into an int **before** revisions are compared. A v1 row and a v2 row with equal values are duplicates.
- [ ] **Never use floats.** Amounts can exceed 2^53, and `amount × num` can exceed int64. Use Python `int`, and store large values as TEXT if SQLite is involved (its INTEGER type is only 64-bit).
- [ ] Money output is signed integer USD microdollars written as JSON integers.
- [ ] **USD has no implicit rate.** USD needs an FX row just like any other currency. In the fixtures USD is 1/2, and it is corrected to 3/2 in batch 9.
- [ ] The FX fraction means microdollars per **minor unit**. Do not do any extra ×100 or ÷100.
- [ ] Timestamps are `YYYY-MM-DDTHH:MM:SSZ`. Plain string comparison is chronological. The FX date is `effective_at[:10]`.

### 2.2 Streams
- [ ] **Ledger** is keyed by `entry_id`. Each revision fully replaces the previous one and may change the date, currency, merchant, customer or amount. `VOID` produces no contribution, no join and no trace, **but it still counts in conflict detection**.
- [ ] **Merchants** are keyed by `merchant_id|valid_from`. The interval `[valid_from, valid_to)` is half-open. A revision replaces the whole interval, including its end. Overlaps between *different* keys are allowed and lead to AMBIGUOUS_MERCHANT.
- [ ] **FX** is keyed by `currency|date`, with `den > 0`.
- [ ] **Profiles**: discard name and email at ingest. They are not needed for anything.
- [ ] **Erasures** mask the token in every snapshot whose read transaction starts after the erasure commits. This is **independent of cutoff B**. Amounts stay unchanged. A tombstone that holds the token is allowed.

### 2.3 Selection and conflicts (§3)
- [ ] Consider only supported rows with `batch <= B`.
- [ ] For each key, pick the **highest `version`**, not the row that arrived last.
- [ ] Collapse identical business payloads at that version. Compare only the declared fields: row_id, batch and schema_version are **not** business fields.
- [ ] If there are ≥2 distinct payloads at the highest version, **block the key**. Emit exactly one `REVISION_CONFLICT` with that version. Do **not** fall back to a lower version, and do not break the tie by row_id.
- [ ] A later, higher, unambiguous version resolves the conflict. Only conflicts at the selected highest version are reported.
- [ ] Report conflicts for **all** streams, even keys nobody references and ledger entries outside the date range.
- [ ] `UNSUPPORTED_SCHEMA` uses: stream exactly as supplied, `key = row_id`, `version = null`, deduplicated by row_id. Include every one with batch ≤ B, whatever its date.

### 2.4 Joins and money (§3)
For each selected, non-blocked **POST** whose date falls in `[start_date, end_date)`:
1. Find the merchant intervals that contain `effective_at`, using only selected, non-blocked keys. Zero matches → `MISSING_MERCHANT`. Two or more → `AMBIGUOUS_MERCHANT`, even if they share the same region and tier.
2. Look up FX for `(currency, date)`, again only selected and non-blocked. None → `MISSING_FX`.
3. Report only the **first** error. It uses `stream=ledger`, `key=entry_id`, and `version=selected version`. An excluded entry gets no contribution and no trace.
4. Compute `usd = round_half_even(amount * num / den)` **per entry**, then sum. Do it with integer math: `q, r = divmod(a*num, den)`. If `2r > den` → `q+1`. If `2r == den` → `q + (q & 1)`. This is symmetric for negative amounts.

### 2.5 Output (§4)
- [ ] The output has exactly four keys: `query_id`, `settlements`, `trace`, `exceptions`.
- [ ] `settlements`: group by `(date, merchant_id, region, tier)` and include `net_usd_micro` and `entry_count`. Keep groups whose net is zero. Sort ascending by the group key.
- [ ] `trace`: one row per contributing entry with its selected `version`, and `customer_token` set to `null` if erased. Sort by `entry_id` as a string, so `L17_E10` comes before `L17_E2`.
- [ ] `exceptions`: sort by `(stream, key, code, version)` with null first. No duplicates.
- [ ] Numbers are JSON ints. No extra fields. Diagnostics go to stderr only, and **must not contain PII**.

### 2.6 Adapter and durability (§5)
- [ ] `./run ingest --input ABS --state ABS [--failpoint after-stage|before-commit]`
- [ ] `./run snapshot --query ABS --state ABS --out ABS`
- [ ] Exit codes: 0 means success. **86** means a failpoint fired on a batch not seen before. **65** means the snapshot needs batches 1..B and one is missing; in that case nothing is published and the existing `--out` file is left byte-for-byte unchanged.
- [ ] `after-stage`: the batch is staged durably but not visible. `before-commit`: we stop immediately before the atomic visibility boundary. Both leave the previous state visible. A replay without a failpoint then commits **exactly once**. **No fake early exit** before durable staging happens.
- [ ] One atomic visibility unit covers the batch payload, revision history, erasure effects and processed-batch checkpoint.
- [ ] Ingesting a batch that is already committed is a no-op that exits 0 (idempotent retry), with or without a failpoint. A *different* file claiming a committed batch number (a different `row_id` set) is rejected with exit 1 and changes nothing.
- [ ] An erasure with an unsupported schema version is quarantined as `UNSUPPORTED_SCHEMA` and does **not** erase. This is settled by §2 ("quarantined"), not an open question. DESIGN.md states the interpretation.
- [ ] A snapshot may use only `--state` and the query. It must not reread input files, use the network or depend on the CWD.
- [ ] Concurrent readers see committed state only. I need to be able to explain the isolation, e.g. SQLite WAL gives each read transaction a consistent snapshot.
- [ ] Output is published atomically: write a temp file **in the same directory** as `--out`, fsync it, then `os.replace`.
- [ ] Docker: the adapter lives at `/app/run` and works for an **arbitrary UID**, a read-only root filesystem, writable `/state` and `/tmp`, and nothing written to `/app`. That means `PYTHONDONTWRITEBYTECODE=1`, `SQLITE_TMPDIR`, and no `__pycache__`. It must also run offline.
- [ ] Pin everything: base image digest and a lockfile. Document the build command.

### 2.7 Scale (§6)
- [ ] 200k ledger keys plus revisions, ≤12 batches, 100 merchants, 4 currencies.
- [ ] Targets: **total ingest < 180 s**, **each snapshot < 30 s**, **peak memory < 4 GiB**, **state < 5 GiB** on a 4-core, 8 GiB host.
- [ ] Record the host, commands, wall time, peak RSS (`/usr/bin/time -v`) and `du -sh state`.
- [ ] Write a ≤600-word note on 1B events/day inside DESIGN.md. It is a design only; do not claim it was built.

### 2.8 Deliverables (README "Submit")
- [ ] A private GitHub repo shared with **`whitecoat-data-team`**. Do not change it after submitting.
- [ ] Source, Dockerfile, `run`, lockfiles, tests.
- [ ] `README.md`: build and run instructions, plus **where the AI logs live**.
- [ ] `DESIGN.md` (≤2,000 words): data model, invariants, state transitions, transaction boundary, time semantics, lineage, alternatives, limits, and the 600-word scale note.
- [ ] `OPERATIONS.md` (≤1,200 words): replay and rebuild, schema change, recovery from a failed run, deletion, observability, performance numbers, and one concrete incident runbook.
- [ ] `AI_LOG.md`: five consequential interactions, what I checked, **≥2 rejected or corrected suggestions** (or an honest statement that there were fewer), the tools and models used, time and cost.
- [ ] **Raw agent logs** committed, with secrets and unrelated private history redacted. This is a hard gate, so **confirm on day one** that export and redaction work.
  - Claude Code (this project): `C:\Users\aman_\.claude\projects\c--Users-aman--Downloads-data-engineer-assessment\*.jsonl`. Copy the files into e.g. `ai-logs/`, then grep them for tokens, emails and paths outside this project before committing.
  - Codex, ChatGPT or any other tool used for this work (including plan reviews): export its sessions too. If a tool can't export, say so in AI_LOG.md and describe what you used instead.
- [ ] `EVIDENCE.md`: exact commands and observed output for correctness, replay, crash recovery, privacy and scale. Keep measured results clearly separate from proposals.
- [ ] `LIMITATIONS.md`: an honest list of what is incomplete.
- [ ] Do not commit bulk data, venvs, secrets or `.env`.

## 3. Traps already visible in the public fixtures

| Batch | What happens | Behaviour to get right |
|---|---|---|
| 1 | Merchants, FX and profiles only | q01 is correctly **empty** |
| 3 | `L17_E0` v2 appears twice with amounts 41 and 43 | `REVISION_CONFLICT` at v2, and E0 is blocked in q03 and q04 |
| 3 | `ledger` schema 99 | `UNSUPPORTED_SCHEMA` with `key=row_id` and `version=null` |
| 3 | E61 has merchant `K17_absent`; E62 has currency `ZZZ` | E61 is `MISSING_MERCHANT` (merchant is checked first even though its FX is also missing); E62 is `MISSING_FX` |
| 4 | E1 v2 `VOID`; new M0 interval starting Jan 2 overlaps v1 [Jan 1, Feb 1) | E1 disappears; M0 entries from Jan 2 onward are `AMBIGUOUS_MERCHANT` |
| 5 | E0 v3 = 99 | Resolves the v2 conflict |
| 5 | `K17_MX` and `XTS` each have two different v1 payloads | Merchant and FX conflicts are reported even though nothing references them |
| 5 | E2 v2 `effective_at = 2026-01-02T00:00:00Z` | Tests the interval boundary and moves the entry's date |
| 6 | E0 v3 sent again as ledger **v2** with `"99"` | A duplicate, not a conflict |
| 7 | M0 v2 shortens the first interval to [Jan 1, Jan 2) | Resolves the ambiguity; E11 and E55 arrive again as exact duplicates |
| 8 | E39, E52 and others: v2 changes the date or sign | Both the old and new date buckets change |
| 9 | USD 2026-01-01 FX v2 = 3/2 | Late FX correction: E0 becomes 99×3/2 = 148.5 → **148** (half-even) |
| 10 | Erasures for C0 and C1 | C0 and C1 are masked even in q01 through q09, because the expected outputs assume all batches were ingested |
| 11 | A profile for C0 is **replayed after its erasure** | Its name and email must never land in state |
| 12 | A new ledger entry for erased C0, plus `future_stream` | Masked, and `UNSUPPORTED_SCHEMA` |

## 4. Architecture (recommended for a 12-hour timebox)

**Python 3.12 standard library plus SQLite.** There are no runtime dependencies, so the lockfile only has to cover dev tools such as pytest. SQLite gives atomic transactions, WAL snapshot isolation for readers, and durability that is easy to inspect.

### State layout (`/state/engine.db`, WAL mode, `synchronous=FULL`, `secure_delete=ON`)
Every table has **explicit typed columns for each stream**. There is no generic JSON or payload column anywhere, so an unexpected field (a profile name, an unknown payload) has nowhere to go.

- `batches(batch PK, committed_at, row_count, row_set_sha256)`: the processed-batch checkpoint and **the commit marker**. A batch is visible if and only if it has a row here.
  - `row_set_sha256` hashes the sorted `row_id`s, **not the raw file**. A raw-file hash would be a fingerprint of unredacted input, including profile names and emails. `row_id`s are opaque, and the `row_id` set identifies the batch, because a reused `row_id` with different bytes is out of scope. It is also stable when a retried file has its lines reordered.
  - Staging first deletes any uncommitted rows for the batch. Without that, rows left behind by a different, abandoned file under the same batch number would be published along with the new file.
- `ledger_rev(entry_id, version, batch, row_id, op, merchant_id, customer_token, effective_at, currency, amount TEXT, canon_hash)`
- `merchant_rev(merchant_id, valid_from, version, batch, row_id, valid_to, region, tier, canon_hash)`
- `fx_rev(currency, date, version, batch, row_id, num TEXT, den TEXT, canon_hash)`
- `unsupported(row_id PK, stream, batch)`: holds these three fields and nothing else.
- `erasures(customer_token, batch, row_id)`: the tombstones.
- Profiles are **dropped during parsing and never written**, including to temp files and logs.

All data tables use `UNIQUE(row_id)` with `INSERT OR IGNORE`. Redelivered rows and replayed batches are therefore idempotent.

**Why there is no separate staging table:** rows are staged directly into the per-stream tables, tagged with their batch number. A staged batch k that has not committed is invisible without any extra filtering:
- A query with cutoff B < k excludes it through `batch <= B`.
- A query with cutoff B ≥ k fails with 65, because k is missing from `batches`.
- Erasures ignore the cutoff, so they are the one exception: they are read via `JOIN batches`.

"Never write PII" beats "write it, then delete it". Deleted SQLite rows can survive in free pages and in the WAL file until a checkpoint or `VACUUM`. `secure_delete` is only a second layer of defence.

### Ingest: two transactions, which gives the two failpoints real meaning
1. Read the file and parse it. Normalise v2 amounts to int, keep only the declared fields, drop profiles, and reduce unsupported rows to `(batch, row_id, stream)`. Compute the canonical hash.
   - **Business-equality rule (as implemented):** each supported stream is parsed into a typed NamedTuple holding **only its declared business fields**, with v2 amounts already converted to int. Tuple equality *is* business equality, so no separate canonical hash is needed. Envelope fields (`row_id`, `batch`, `schema_version`), extra fields, profiles and unknown payloads are never part of the tuple.
   - Tested: v1 `99` and v2 `"99"` are equal, and extra payload or envelope fields don't change equality (`tests/test_records.py`).
   - Storage consequence: revision selection runs in Python over rows loaded with `batch <= B`, sharing one code path with the in-memory reference `engine.core.evaluate`. If the scale tests show loading is too slow, add a hash column and push the selection down into SQL.
2. If `batch` is already in `batches`, exit 0 and do nothing, even if a failpoint was given.
3. **Txn A (stage)**: `INSERT OR IGNORE` the rows into the per-stream tables, then COMMIT, which makes them durable. → `--failpoint after-stage` exits 86 here. The rows are durable but invisible.
4. **Txn B (publish)**: `BEGIN IMMEDIATE`, then `INSERT INTO batches`. → `--failpoint before-commit` exits 86 immediately before `COMMIT`, which rolls back. Otherwise, COMMIT. This single-row commit is the atomic visibility boundary for the batch payload, revision history, erasure effects and the checkpoint.
5. A replay restarts from step 1. `INSERT OR IGNORE` in txn A is a no-op for rows already staged, and txn B commits exactly once.
6. DESIGN.md must explain why the two failpoints are distinct: one comes after a durable commit, the other falls inside an open transaction.

### Snapshot: one read transaction, full recompute
1. `BEGIN` (a deferred read transaction; WAL gives a consistent snapshot). Everything is read inside it, including erasures, which are joined to `batches` so that only committed ones count.
2. Check that `batches` contains every value 1..B. If not, exit 65 and leave the output untouched.
   - **Invariant:** once this check passes, every row with `batch <= B` belongs to a committed batch. That is why revision reads can filter on `batch <= B` without joining `batches`.
   - **The gap check is therefore load-bearing for correctness, not just input validation.** Say so in a code comment next to the check and in DESIGN.md, and guard it with the staged-batch invisibility test (criterion 9).
   - Erasures are the only read that ignores B, so they always join `batches`.
3. For each stream, select rows with `batch <= B`, group by key, take the max version, and count distinct `canon_hash` at that version. That produces the selected set and the conflict list. This is doable in SQL with window functions, or in Python.
4. Do the joins in Python using a dictionary of merchant intervals, the FX dictionary and integer math.
5. Serialise, write to a temp file next to `--out`, fsync, then `os.replace`.

Full recompute on 200k keys should take only a few seconds. The spec allows it as long as I **measure and explain its limits**. A stretch goal is a materialised "latest per key" cache that only helps when B = max. It is probably not worth building.

## 5. Build order with a time budget (~12 h)

| # | Step | Budget | Done when |
|---|---|---|---|
| pre | **Prerequisites, before the clock starts:** install Docker Desktop (WSL2 backend), check that `docker run --rm hello-world` works, and confirm that the agent logs can be exported | outside the 12 h | `docker version` works |
| 0 | `git init`, `.gitignore`, `.gitattributes` (`run text eol=lf`), note the start time | 0.25 h | Repo exists |
| 1 | Pure core: parse and normalise, canonical hash, half-even rounding, selection and conflicts, joins, output formatting. Unit tests on WORKED_EXAMPLES 1–7 | 2.5 h | Worked-example tests pass |
| 2 | SQLite state, ingest with its two transactions, snapshot, CLI `run` | 2 h | Ingest 1..12 → all 10 `expected/*.json` match **exactly** |
| 3 | **Dockerfile, done early because it is the biggest schedule risk** (digest-pinned, non-root). Test with `docker run --read-only --user 12345 --network none --tmpfs /tmp -v ...` | 1 h | Public fixtures pass inside the container |
| 4 | Durability tests: both failpoints followed by replay, snapshot while a batch is staged but not published, retrying the same batch, reordered ingest with interim 65 checks, gap → 65 with `--out` unchanged, restart | 1.5 h | Test suite is green |
| ★ | **Milestone gate, non-negotiable, in two halves.** *Functional (host):* public output matches, both failpoints recover, gap rejection works. *Release (Linux container):* the same, plus the `run` shell adapter, an arbitrary UID, a read-only root filesystem, WAL access under `--read-only`, and directory fsync semantics. **Only a container run counts as passing the release half.** Nothing below starts until both hold, except scale work that doesn't need Docker. If time runs short, cut from the steps below and record the cuts in LIMITATIONS.md | — | |
| 5 | Privacy: grep the state dir, WAL files and stderr for `SYNTHETIC_` and `example.invalid`; check that erasure masks old cutoffs | 0.75 h | No hits |
| 6 | Scale generator (seeded, gitignored output), measure ingest, snapshot, RSS and disk | 1.25 h | Numbers inside targets, written to EVIDENCE |
| 7 | Docs: README, DESIGN, OPERATIONS, AI_LOG, EVIDENCE, LIMITATIONS, plus the raw logs | 2.25 h | Word limits respected |
| — | Buffer | 0.5 h | |

**Local environment notes:** Docker is **not installed** on this machine, so install Docker Desktop (WSL2 backend) as a prerequisite, before implementation begins. Record the install time separately in AI_LOG.md so the active-hours figure stays honest. The local Python is 3.14, so pin the image to 3.12 or 3.13 and test there. On Windows, **line endings in `run` will break the shebang**, and the executable bit has to be set with `git update-index --chmod=+x run`.

## 6. Success criteria (how I know I'm done)

**Correctness (hard gate)**
1. Ingest batches 1..12 into a fresh state, then run all 10 queries. Each output is JSON-equal to `expected/<q>.json`, and `check_output.py` passes.
2. Run ≥20 random ingest orders of all 12 batches. After each ingest, run every query:
   - Any query whose cutoff B is not yet covered by batches 1..B must **exit 65** and leave `--out` unchanged.
   - Any query that is covered must already **equal its expected output**. Only the erasure masking can differ, and only before batch 10 arrives.
   - After all batches arrive, every output matches the expected output, including when each batch is ingested twice.
3. Unit tests cover WORKED_EXAMPLES 1–7, including negative ties (−1/2 → 0, −3/2 → −2) and 9,007,199,254,740,993 × 1/1.
4. Mutation sensitivity: breaking any single rule (tie-breaking, `<` vs `<=` on `valid_to`, fallback to a lower version, float parsing, v2 normalisation, join-error order) makes at least one test fail. Try each by hand once.

**Recovery (hard gate)**
5. `--failpoint after-stage` and `before-commit` on a new batch → exit 86. A snapshot afterwards matches the state from before that batch. Replaying without the failpoint → exit 0, and the batch is committed once (row counts do not change on a second replay).
6. A failpoint on an already-committed batch does not produce 86. It is a no-op that exits 0.
7. Snapshot with cutoff B while batch k ≤ B is missing → exit 65, and an existing `--out` file keeps the same sha256.
8. Killing the process (`kill -9`) at random points during ingest never leaves a partially visible batch.
9. **Staged batches are invisible.** Commit batches 1..k−1, then stage batch k with `--failpoint after-stage`.
   - A snapshot with B < k gives exactly the same bytes as before the staging.
   - A snapshot with B ≥ k exits 65.
   - An erasure in batch k does **not** mask anything yet.
   - Repeat the test with `before-commit`.

**Privacy (hard gate)**
10. `grep -r "SYNTHETIC_\|example.invalid" state/` (including `-wal` and `-shm`) finds nothing, both after all batches and after the batch-11 replay. Stderr contains no PII. Also run the check on a state where an unsupported-schema row carried fake PII in its payload.
11. Once batch 10 has committed, C0 and C1 are `null` in the trace for **every query cutoff, including q01–q09**. Amounts are unchanged.

**Operational**
12. The Docker image builds with pinned versions, runs as a random UID with `--read-only` and `--network none`, and writes only to `/state`, `/tmp` and the output mount.
13. Measured scale results: ingest < 180 s, snapshot < 30 s, RSS < 4 GiB, state < 5 GiB, recorded in EVIDENCE.md along with the host specs.

**Submission**
14. All **six** required docs (README, DESIGN, OPERATIONS, AI_LOG, EVIDENCE, LIMITATIONS) are present and within their word limits, **plus** the committed raw agent logs. There are no secrets or bulk data. The repo is private and `whitecoat-data-team` is invited.

## 7. What to take away (whatever the outcome)
- **Bitemporal modelling**: keep append-only revisions tagged with knowledge time, and derive "as-of" views from them instead of mutating current state.
- **Determinism**: conflicts must be surfaced, never resolved by arrival order or ID. Canonicalise before comparing.
- **Money**: integers only, round each entry, and know exactly what banker's rounding does with negative numbers.
- **The transaction boundary is the design.** Staging vs publish, idempotency keys and checkpoints must sit inside the same atomic unit.
- **Privacy by construction**: the cheapest erasure is never storing the data. Masking happens at read time from a global tombstone set.
- **Evidence over claims**: every statement in the docs should have a command and output behind it.
