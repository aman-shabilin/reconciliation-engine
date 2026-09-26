#!/usr/bin/env python3
"""Mutation check: each mutant is a plausible spec violation; the suite must fail on every one.

Copies the repo to a temp dir per mutant, applies one exact text replacement
to engine source, runs the unit tests, and reports survivors. Exit 1 if any
mutant survives or any replacement target is not found exactly once.

    python3 scripts/mutants.py
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (description, file, old, new)
MUTANTS = [
    ("round half up instead of half even", "engine/money.py",
     "twice == denominator and q & 1", "twice == denominator"),
    ("round half away from zero via >=", "engine/money.py",
     "twice > denominator or", "twice >= denominator or"),
    ("float division", "engine/money.py",
     "return div_round_half_even(amount_minor * num, den)",
     "return int(round(amount_minor * num / den))"),
    ("v2 amount parsed through float", "engine/records.py",
     "return int(text)", "return int(float(text))"),
    ("bool accepted as schema version", "engine/records.py",
     "type(schema_version) is int", "isinstance(schema_version, int)"),
    ("last-arriving version wins ties", "engine/core.py",
     "r.version > cur[0]", "r.version >= cur[0]"),
    ("conflict picks an arbitrary winner", "engine/core.py",
     "if len(variants) == 1:", "if len(variants) >= 1:"),
    ("lowest version wins", "engine/core.py",
     "if cur is None or r.version > cur[0]:", "if cur is None or r.version < cur[0]:"),
    ("interval end inclusive", "engine/core.py",
     "m.valid_from <= e.effective_at < m.valid_to", "m.valid_from <= e.effective_at <= m.valid_to"),
    ("interval start exclusive", "engine/core.py",
     "m.valid_from <= e.effective_at < m.valid_to", "m.valid_from < e.effective_at < m.valid_to"),
    ("ambiguity ignored (first match wins)", "engine/core.py",
     "if len(matches) > 1:", "if len(matches) > 99:"),
    ("date range end inclusive", "engine/core.py",
     "query.start_date <= date < query.end_date", "query.start_date <= date <= query.end_date"),
    ("VOID contributes", "engine/core.py",
     'if e.op != "POST":', "if False:"),
    ("erasure not applied", "engine/core.py",
     "None if e.customer_token in erased else", "None if False else"),
    ("erasure limited by cutoff", "engine/core.py",
     "erased.add(r.customer_token)  # independent of cutoff",
     "erased.add(r.customer_token) if row.batch <= query.cutoff_batch else None"),
    ("null version sorts last", "engine/core.py",
     "(0, 0) if version is None", "(2, 0) if version is None"),
    ("ledger conflicts limited to date range", "engine/core.py",
     'exceptions.update(("ledger", k, REVISION_CONFLICT, v) for k, v in conflicts)',
     'exceptions.update(("ledger", k, REVISION_CONFLICT, v) for k, v in conflicts '
     'if query.start_date <= sel_ledger.get(k, LedgerRev(*[""] * 8)).effective_at[:10] < query.end_date)'),
    ("fx keyed by query start not entry date", "engine/core.py",
     "rates.get((e.currency, date))", "rates.get((e.currency, query.start_date))"),
    ("gap check disabled", "engine/store.py",
     "if present != max(cutoff, 0):", "if False:"),
    ("uncommitted erasures applied", "engine/store.py",
     '"JOIN batches USING (batch)")]', '"")]'),
    ("before-commit failpoint fires after COMMIT", "engine/store.py",
     'crash(86)\n            conn.execute("ROLLBACK")',
     'conn.execute("COMMIT")\n            crash(86)'),
    ("fake after-stage exit before staging", "engine/store.py",
     "counts = _stage(conn, rows)",
     "counts = _stage(conn, rows) if failpoint != 'after-stage' else crash(86)"),
    ("replay of committed batch re-commits", "engine/store.py",
     "        if prior is not None:\n", "        if False:\n"),
    ("stale staged rows kept on re-stage", "engine/store.py",
     "        _discard_uncommitted(conn, batch)\n", ""),
    ("different file under committed batch accepted", "engine/store.py",
     "if prior != fingerprint:", "if False:"),
    ("fingerprint depends on line order", "engine/store.py",
     'joined = "\\n".join(sorted({r.row_id for r in rows}))',
     'joined = "\\n".join(r.row_id for r in rows)'),
    ("snapshot writes output before gap check can fail", "engine/cli.py",
     "        report = store.load_report(args.state.resolve(), query)\n",
     "        args.out.resolve().write_bytes(b'partial')\n"
     "        report = store.load_report(args.state.resolve(), query)\n"),
    # Not listed: removing evaluate()'s unsupported-row dedupe is an equivalent
    # mutant, because build_report collects exceptions in a set.
    ("MISSING_FX checked before merchant", "engine/core.py",
     "        if not matches:\n", "        if rates.get((e.currency, date)) is None:\n"
     "            exceptions.add((\"ledger\", e.entry_id, MISSING_FX, e.version))\n"
     "            continue\n        if not matches:\n"),
]

COPY = ["engine", "tests", "data", "queries", "expected"]


def run_suite(tmp: Path):
    """Run the suite fail-fast; return (passed, first failing test id or None)."""
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-f", "-s", "tests", "-t", "."],
        cwd=tmp, capture_output=True, text=True)
    first = next((line.split(":", 1)[1].strip() for line in result.stderr.splitlines()
                  if line.startswith(("FAIL:", "ERROR:"))), None)
    return result.returncode == 0, first


def copy_repo(tmp: Path) -> None:
    for name in COPY:
        shutil.copytree(ROOT / name, tmp / name, ignore=shutil.ignore_patterns("__pycache__"))


def main() -> int:
    # Baseline: an unmutated copy must pass, or every "kill" is meaningless.
    with tempfile.TemporaryDirectory() as tmp:
        copy_repo(Path(tmp))
        passed, first = run_suite(Path(tmp))
        if not passed:
            print(f"BASELINE FAILED ({first}); mutation results would be invalid")
            return 1
    print("baseline passes\n")

    survivors, broken = [], []
    for desc, rel, old, new in MUTANTS:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            copy_repo(tmp)
            target = tmp / rel
            src = target.read_text(encoding="utf-8")
            if src.count(old) != 1:
                broken.append(desc)
                print(f"BROKEN   {desc}: target found {src.count(old)} times")
                continue
            target.write_text(src.replace(old, new), encoding="utf-8")
            passed, first = run_suite(tmp)
            if passed:
                survivors.append(desc)
                print(f"SURVIVED {desc}")
            else:
                print(f"killed   {desc}\n           by {first}")
    print(f"\n{len(MUTANTS) - len(survivors) - len(broken)}/{len(MUTANTS)} mutants killed")
    return 1 if survivors or broken else 0


if __name__ == "__main__":
    sys.exit(main())
