# AI usage log

> Running draft. Entries are added as work happens; `TODO(me)` marks facts only I can supply.

## Tools and models

| Tool | Model | Used for | Raw logs |
|---|---|---|---|
| Claude Code (VS Code extension) | Claude Opus 5.5 (`claude-opus-5-5`) | Assessment reading, planning, implementation, tests | [`ai-logs/claude-code/`](ai-logs/claude-code/) |
| TODO(me): the reviewer tool (ChatGPT / Codex / other?) | TODO(me) | Independent review of the plan and progress | TODO(me): export location, or a statement that it cannot export |

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

### 3. External review of the plan, round 2 (TODO(me): tool) — suggestion corrected
- **Corrected suggestion:** the reviewer said to replace the Claude log path with "the actual Codex log-export location". That was wrong for this project, because the implementation work happens in Claude Code. The Claude log path was kept, and exporting logs from the reviewer tool was added as a separate requirement.
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

## Suggestions rejected or corrected
1. The reviewer's instruction to use the Codex log path, corrected as described in interaction 3.
2. Claude's own first design, a generic `canon_json` staging table, rejected after review because it could retain PII (interaction 2).
3. Claude's own first plan, a canonical hash column, replaced by typed-tuple equality (interaction 4).
4. Claude's first mutant list included one mutant that can never be detected, so it was removed (interaction 5).
