# Reconciliation Engine

A local, recoverable settlement engine: ingests JSONL batches into durable SQLite state and answers
knowledge-time/effective-time settlement queries with exact integer money. Python 3 standard library only.

> Status: implementation, tests, scale measurements and docs are complete on the dev host. The Linux container
> release gate has **not been run yet** (see [LIMITATIONS.md](LIMITATIONS.md)).

**Documents:** [DESIGN.md](DESIGN.md) · [OPERATIONS.md](OPERATIONS.md) · [EVIDENCE.md](EVIDENCE.md) (raw outputs in
[evidence/](evidence/)) · [LIMITATIONS.md](LIMITATIONS.md) · [AI_LOG.md](AI_LOG.md)

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

## Docker

```sh
docker build -t recon-engine .
docker run --rm --read-only --user 12345:12345 --network none --tmpfs /tmp \
  -v "$PWD/data:/data:ro" -v recon-state:/state -v "$PWD/out:/out" \
  recon-engine ingest --input /data/batch_01.jsonl --state /state
docker run ... recon-engine /app/run snapshot --query /data/q.json --state /state --out /out/q.json
docker run --rm --read-only --tmpfs /tmp recon-engine selftest    # full test suite inside the image
python3 scripts/container_check.py                                # release-gate checks (see EVIDENCE.md)
```

The adapter is `/app/run`. The entrypoint accepts either `ingest|snapshot ...` directly or an explicit `/app/run ...`.
The base image is pinned by digest, and there are no third-party packages ([requirements.lock](requirements.lock)), so the
image runs offline. `/app` is root-owned and read-only to the runtime user; only `/state`, `/tmp` and the output mount are written.

## Tests

```sh
python3 -m unittest discover -s tests -t .   # unit, public-fixture parity, durability, privacy
python3 scripts/mutants.py                   # injects 28 spec violations; all must be caught
python3 scripts/gen_scale.py                 # seeded 200k-key scale fixture into scale/ (gitignored)
python3 scripts/bench.py --verify            # ingest/snapshot wall time, peak RSS, state size
python3 scripts/crash_test.py --trials 40    # hard-kill ingest at random points, check recovery
```

## Layout
- `engine/records.py` parsing/normalisation · `engine/money.py` exact rounding · `engine/core.py` selection, joins, output ·
  `engine/store.py` SQLite state and transactions · `engine/cli.py` adapter behind `./run`
- Assignment brief (unmodified): [docs/assignment/](docs/assignment/README.md) · Working plan: [PLAN.md](PLAN.md)

## AI usage and logs
- Summary, verification and rejected suggestions: [AI_LOG.md](AI_LOG.md)
- Raw Claude Code session logs (JSONL, credentials and personal email redacted): [ai-logs/claude-code/](ai-logs/claude-code/)
- Re-export the latest logs with `python3 scripts/export_ai_logs.py`
