# Reconciliation Engine

A local, recoverable settlement engine: ingests JSONL batches into durable SQLite state and answers
knowledge-time/effective-time settlement queries with exact integer money. Python 3 standard library only.

> Work in progress: Docker image, scale measurements and design/operations docs are not done yet.

## Run locally

Requires Python ≥ 3.9 on PATH as `python3` (override with `PYTHON=...`, e.g. `PYTHON=python` on Windows Git Bash).

```sh
./run ingest   --input /abs/data/batch_01.jsonl --state /abs/state
./run ingest   --input /abs/data/batch_02.jsonl --state /abs/state --failpoint after-stage   # exits 86
./run snapshot --query /abs/queries/q12.json   --state /abs/state --out /abs/out/q12.json
```

Exit codes: `0` success · `65` snapshot cutoff B not covered by committed batches 1..B (existing `--out` untouched) ·
`86` failpoint fired · `2` usage error · `1` rejected input (e.g. a different file claiming an already-committed
batch number) or other failure. Stderr never carries payload values; there is no debug mode that relaxes this.

## Tests

```sh
python3 -m unittest discover -s tests -t .   # unit, public-fixture parity, durability, privacy
python3 scripts/mutants.py                   # injects 25 spec violations; all must be caught
```

## Layout
- `engine/records.py` parsing/normalisation · `engine/money.py` exact rounding · `engine/core.py` selection, joins, output ·
  `engine/store.py` SQLite state and transactions · `engine/cli.py` adapter behind `./run`
- Assignment brief (unmodified): [docs/assignment/](docs/assignment/README.md) · Working plan: [PLAN.md](PLAN.md)

## AI usage and logs
- Summary, verification and rejected suggestions: [AI_LOG.md](AI_LOG.md)
- Raw Claude Code session logs (JSONL, credentials and personal email redacted): [ai-logs/claude-code/](ai-logs/claude-code/)
- Re-export the latest logs with `python3 scripts/export_ai_logs.py`
