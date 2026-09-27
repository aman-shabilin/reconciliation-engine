# AI usage log

> Running draft. Entries are added as work happens; `TODO(me)` marks facts only I can supply.

## Tools and models

| Tool | Model | Used for | Raw logs |
|---|---|---|---|
| Claude Code (VS Code extension) | Claude Opus 5.5 (`claude-opus-5-5`) | Assessment reading, planning, implementation, tests | [`ai-logs/claude-code/`](ai-logs/claude-code/) |

**Export and redaction.** `python3 scripts/export_ai_logs.py` copies the Claude Code session JSONL files from `~/.claude/projects/<project>/` into `ai-logs/claude-code/`. It redacts credential patterns (API keys, tokens, bearer headers, private keys) and my personal email address. Nothing else is removed. The script fails if a credential-like string survives or a line stops being valid JSON. The first run found no credentials, only my email address. The fixture's synthetic `@example.invalid` addresses stay in the logs because they are part of the published fixtures.

**Time and cost.** TODO(me): active hours per session and approximate spend. Docker Desktop installation is recorded separately and is not counted in the 12-hour engineering timebox.

## Consequential interactions

### 1. Reading the brief and drafting the plan (Claude Code)
- **Asked:** assess the repo and README, then write a working plan and success criteria covering every requirement.
- **Produced:** `PLAN.md`, containing a requirement checklist, a table of fixture traps, the SQLite architecture, the build order and success criteria.
- **Checked:** the fixture-trap table was derived by inspecting every batch file and all public expected outputs. For example, the USD FX rate is 1/2 in batch 1 and corrected to 3/2 in batch 9, so `L17_E0` = 99 × 3/2 = 148.5 → 148 (ties to even). The trace in `expected/q12.json` confirms this.

### 2. External review of the plan, round 1 (TODO(me): tool)
The reviewer raised seven points. Claude checked each one against the spec and all seven held:
- **Accepted, and it caught a real privacy flaw:** the generic `staged(canon_json)` table would briefly have held unknown-schema payloads, which may contain PII. Deleted SQLite rows can survive in free pages and in the WAL.
- **Accepted:** "seven documents" was a miscount. There are six documents plus the raw logs.
- **Accepted:** the success criterion said "every cutoff after batch 10". It should say every cutoff, including q01–q09, once batch 10 has committed.
- **Accepted:** the random-ingest-order test must also expect exit 65 in the interim, until batches 1..B are present.
- **Accepted:** add a test that a staged but unpublished batch is invisible to snapshots.
- **Accepted:** move Docker earlier in the build order, since it is the largest schedule risk.
- **Adapted:** instead of per-stream staging tables, Claude proposed staging directly into the per-stream revision tables and using the `batches` row as the commit marker. A staged batch is then invisible by construction: `batch <= B` filters it out for smaller cutoffs, and the gap check returns 65 for larger ones.

### 3. External review of the plan

- **Accepted:** the canonical-hash rule was made explicit, and the gap check was documented as essential to correctness, not just input validation.
- **Accepted:** Docker installation was moved out of the 0.25-hour setup step and treated as a prerequisite.
- **Accepted:** logs must be exportable early. That is why `scripts/export_ai_logs.py` exists.

### 4. Implementing the pure core (Claude Code)
- **Produced:** `engine/records.py`, `engine/money.py`, `engine/core.py` and 60 unit tests.
- **Checked:**
  - All 10 public queries match `expected/` exactly.
  - Results are unchanged under 10 random row orders and when every row is duplicated.
  - Rounding matches Python's exact `Fraction` half-even rounding on 25,000 random cases.
- **Plan item revised:** the planned canonical hash was dropped. Each stream is parsed into a typed tuple holding only its declared fields, so tuple equality already is business equality. `PLAN.md` was updated to say so.

### 5. Mutation testing the suite (Claude Code) — AI output partly rejected
- `scripts/mutants.py` injects 20 plausible spec violations, for example half-up rounding, an inclusive `valid_to`, float parsing, the last-arriving row winning, and erasure bounded by the cutoff.
- **Two mutants survived on the first run.** Both were analysed rather than papered over:
  - "Null version sorts last" can never be observed through a report, because no two exceptions share stream, key and code while differing only in version. A direct unit test was added for the sort key.
  - Removing the unsupported-row dedupe is an equivalent mutant, because exceptions are collected in a set. The mutant was removed from the list, with a comment explaining why.
- **Result:** 19/19 killed.

### 6. Storage layer, CLI and durability tests (Claude Code) — AI tooling bug caught
- **Produced:** `engine/store.py` (two-transaction ingest into SQLite WAL, snapshot inside one read transaction, atomic output), `engine/cli.py`, `./run` and 16 durability tests.
- **Checked:**
  - All 10 public queries match through `./run` against real persisted state.
  - Grepping the state directory, including the WAL, finds no fixture names or emails.
  - 20 random ingest orders with whole-file retries: after every single ingest, each query either exits 65 (batches 1..B incomplete) or equals the in-memory reference over the committed batches.
  - Real subprocess failpoints exit 86 and replay commits once.
  - A reader that started before an erasure commits keeps its snapshot.
- **AI mistake caught:** when storage mutants were added, Claude also added unittest's fail-fast flag in the wrong position (`unittest -f discover`). Every mutant then "failed" because `discover` was treated as a missing test module, so the result showed 25/25 killed in 3 seconds. The implausible runtime prompted a check. The harness now runs an unmutated baseline first and prints the test that killed each mutant. After the fix: 25/25 killed, each with a named test. The earlier 19/19 result is unaffected, because it predates the flag.

### 7. External review of the storage layer — Claude's design choices corrected
The reviewer challenged five points. All were accepted, one with an adaptation:
- **Raw-file SHA-256 in the checkpoint** was a fingerprint of unredacted input, including profile PII. It was replaced by a hash of the sorted `row_id` set.
- **Different file under a committed batch number:** it had been logged and accepted silently. It is now rejected with exit 1 and no change.
- **Adapted:** the two points above were solved together. The `row_id`-set fingerprint carries no PII, detects a genuinely different file, and still accepts a retry whose lines are reordered, which a raw-file hash would have wrongly rejected.
- **A real bug found while doing this:** rows left over from a staged but never-committed file would have been published when a different file with the same batch number committed. Staging now first discards the batch's uncommitted rows.
- **`RECON_DEBUG` traceback mode removed.** It contradicted the "stderr carries no PII" guarantee.
- **Unsupported-schema erasure behaviour:** Claude had listed it as an open question for the hiring team. The spec's "quarantined" settles it, so the question was withdrawn.
- **Milestone wording:** Claude had said the gate "passes except Docker". That was overstated. The plan now separates a functional gate, which passes on the host, from a release gate that only a Linux container run can pass.
- **Checked:** 80 tests pass. 28/28 mutants are killed, including three new ones: stale staged rows kept, a different committed file accepted, and a fingerprint that depends on line order.

### 8. Scale, hard-kill testing and documentation (Claude Code): a false alarm investigated
- **Produced:**
  - `scripts/gen_scale.py`, `scripts/bench.py` and `scripts/crash_test.py`
  - DESIGN, OPERATIONS, EVIDENCE and LIMITATIONS
- **The first run of the crash test reported a violation.** Claude traced it before trusting either side: the *test* stored its reference snapshots by cutoff only, so the batch-12 setup (which commits erasures) overwrote the cutoff-11 reference with a masked version. The engine's unmasked output was correct for a state with no committed erasure. References are now keyed by (target batch, cutoff). After the fix, 30 trials and then 40 trials passed. The engine was not changed.
- **Checked:**
  - Every scale query equals the in-memory oracle.
  - Exception counts confirm that every edge-case family appears in the scale data.
  - Word limits: DESIGN 1,792 words with a 540-word scale note; OPERATIONS 1,113.
  - EVIDENCE labels the container gate NOT MEASURED instead of inferring the result.

### 9. Linux container release gate (Claude Code): a coverage gap noticed despite a green run
- **Produced:** `scripts/container_check.py`, run against the digest-pinned image. Result: all checks passed (EVIDENCE.md §7), including 80/80 self-tests with the `run` adapter test executing, and 200k-key performance under `--cpus 4 --memory 8g`.
- **Not accepted at face value:** the timed SIGKILL test passed, but its outcome counts showed no kill had landed between staging and commit on Linux. A late-window retry still hit parsing 28 of 30 times. Claude added `--kill-when-staged`, which polls the state read-only and kills the instant staged rows appear. That run produced 30/30 kills in the intended window, with no violations.
- **Checked:** the docs changed from "NOT MEASURED" to the observed values. LIMITATIONS.md now states the remaining boundaries: a VM rather than a bare host, local filesystems only, and no power-loss testing.

### 10. Overstated completion claim, corrected after the user challenged it
- **What happened:** after the container check passed, Claude edited `container_check.py`, adding the state-triggered kill stage, and `crash_test.py`, adding `--window` and `--kill-when-staged`. It then wrote "Status: complete" in the README and presented "ALL CONTAINER CHECKS PASSED" as evidence. The committed scripts had never run end to end. Claude mentioned this only as a closing note. The user asked why the README claimed completion.
- **Correction:**
  - Re-ran the committed version end to end.
  - The first re-run's header said "working tree DIRTY". Claude investigated rather than dismissing it. The cause was the capture itself: the shell redirect truncated the tracked evidence file before `git status` ran. Confirmed by `git status` (that file was the only change) and by hashing all 50 files in the tested image against the commit (all identical).
  - Re-ran again with output captured outside the repo. The header now reads `commit 9ee1ce5, working tree clean`, and the run passed.
  - EVIDENCE §7 now cites that run's figures, which differ slightly from the earlier run (for example, ingest 4.9 s rather than 4.1 s). Pre-commit runs are labelled as supporting evidence only. The README states the verified commit and gives a command to confirm that later commits touch only docs and evidence.
- **Lesson applied:** a status claim must come from a run of the exact committed code. Evidence headers now record the commit and whether the tree was clean.

## Suggestions rejected or corrected

2. Claude's own first design, a generic `canon_json` staging table, rejected after review because it could retain PII (interaction 2).
3. Claude's own first plan, a canonical hash column, replaced by typed-tuple equality (interaction 4).
4. Claude's first mutant list included one mutant that can never be detected, so it was removed (interaction 5).
5. Claude's mutation harness reported false kills because of a misplaced `-f` flag. The result was rejected and the harness fixed and hardened with a baseline run (interaction 6).
6. Claude's checkpoint design, a raw-file SHA-256 with silent acceptance of a different file, was corrected after review (interaction 7).
7. Claude's plan to ask the hiring team about unsupported-schema erasures was withdrawn, because the spec already settles it (interaction 7).
8. Claude's "complete" / "all checks passed" claim for scripts that had changed since the run was corrected by re-running the committed code (interaction 10).
