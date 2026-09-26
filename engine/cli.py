"""Command-line adapter behind ./run.

Exit codes: 0 success, 65 snapshot cutoff not covered by committed batches
1..B, 86 failpoint fired, 2 usage error, 1 any other failure.

Diagnostics go to stderr and never include payload values. Unexpected
errors print only the exception type and location, never the message or a
traceback, because either can echo input values. There is deliberately no
debug switch that relaxes this; debug with the test suite instead.
"""
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

from . import store
from .core import parse_query

EXIT_GAP = 65
EXIT_FAILPOINT = 86


def log(message: str) -> None:
    print(f"engine: {message}", file=sys.stderr, flush=True)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run")
    sub = p.add_subparsers(dest="command", required=True)
    ing = sub.add_parser("ingest", help="ingest one JSONL batch file")
    ing.add_argument("--input", required=True, type=Path)
    ing.add_argument("--state", required=True, type=Path)
    ing.add_argument("--failpoint", choices=store.FAILPOINTS)
    snap = sub.add_parser("snapshot", help="answer one query from committed state")
    snap.add_argument("--query", required=True, type=Path)
    snap.add_argument("--state", required=True, type=Path)
    snap.add_argument("--out", required=True, type=Path)
    return p


def _ingest(args) -> int:
    store.ingest(args.input.resolve(), args.state.resolve(), args.failpoint,
                 crash=lambda code: os._exit(code), log=log)
    return 0


def _snapshot(args) -> int:
    query = parse_query(json.loads(args.query.resolve().read_text(encoding="utf-8")))
    try:
        report = store.load_report(args.state.resolve(), query)
    except store.GapError as e:
        log(f"snapshot {query.query_id}: {e}; existing output left untouched")
        return EXIT_GAP
    data = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    store.write_atomic(args.out.resolve(), data)
    log(f"snapshot {query.query_id}: {len(report['settlements'])} settlements, "
        f"{len(report['trace'])} trace, {len(report['exceptions'])} exceptions")
    return 0


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _ingest(args) if args.command == "ingest" else _snapshot(args)
    except (store.IntegrityError, store.InputError) as e:
        log(f"{args.command} rejected: {e}")  # messages built from batch numbers only
        return 1
    except Exception as e:  # noqa: BLE001 - last-resort, PII-safe reporting
        frame = traceback.extract_tb(e.__traceback__)[-1]
        log(f"{args.command} failed: {type(e).__name__} at "
            f"{Path(frame.filename).name}:{frame.lineno}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
