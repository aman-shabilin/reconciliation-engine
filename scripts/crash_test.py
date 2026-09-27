#!/usr/bin/env python3
"""Hard-kill ingest at random points and check the recovery invariants.

    python3 scripts/gen_scale.py                  # once
    python3 scripts/crash_test.py [--trials 30] [--seed 1]

Setup: commit batches 1..10 of the scale fixture into a template state and
record reference snapshots. Each trial copies the template, starts ingest
of batch 11 or 12 (batch 12 carries erasures), kills it with SIGKILL
(TerminateProcess on Windows) after a random delay spanning the whole
ingest, then checks:

  1. PRAGMA integrity_check is ok;
  2. the batch is either fully committed or invisible: the snapshot at the
     previous cutoff is byte-identical to the reference, and the snapshot
     at the new cutoff is either the full reference or exit 65;
  3. replaying the batch without a failpoint exits 0, and afterwards the
     snapshot at the new cutoff is byte-identical to the reference.
Exits 1 on any violation.
"""
import argparse
import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
CMD = [str(ROOT / "run")] if os.name == "posix" else [sys.executable, "-B", "-m", "engine.cli"]


def engine(*args):
    return subprocess.run(CMD + [str(a) for a in args], env=ENV, capture_output=True, text=True)


def snapshot(state, query, out):
    if out.exists():
        out.unlink()
    r = engine("snapshot", "--query", query, "--state", state, "--out", out)
    return r.returncode, (out.read_bytes() if r.returncode == 0 else None)


def write_query(tmp, cutoff):
    q = tmp / f"q{cutoff}.json"
    q.write_text(json.dumps({"query_id": f"c{cutoff}", "cutoff_batch": cutoff,
                             "start_date": "2026-01-10", "end_date": "2026-01-20"}))
    return q


def hard_kill(p):
    if os.name == "posix":
        os.kill(p.pid, signal.SIGKILL)
    else:
        p.kill()  # TerminateProcess: no cleanup handlers run
    p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "scale")
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    batch = {b: (args.data / f"batch_{b:02d}.jsonl").resolve() for b in range(1, 13)}
    if not batch[1].exists():
        sys.exit("scale fixture missing; run scripts/gen_scale.py")
    rng = random.Random(args.seed)
    tmp = Path(tempfile.mkdtemp(prefix="recon-crash-"))
    try:
        template = tmp / "template"
        for b in range(1, 11):
            assert engine("ingest", "--input", batch[b], "--state", template).returncode == 0
        queries = {c: write_query(tmp, c) for c in (10, 11, 12)}
        ref, duration = {}, {}
        for target in (11, 12):
            full = tmp / f"full{target}"
            shutil.copytree(template, full)
            if target == 12:
                assert engine("ingest", "--input", batch[11], "--state", full).returncode == 0
            t0 = time.perf_counter()
            assert engine("ingest", "--input", batch[target], "--state", full).returncode == 0
            duration[target] = time.perf_counter() - t0
            # Keyed by (target, cutoff): batch 12 commits erasures, which mask
            # tokens at every cutoff, so references differ per final state.
            ref[target, target] = snapshot(full, queries[target], tmp / "ref.json")[1]
            ref[target, target - 1] = snapshot(full, queries[target - 1], tmp / "ref.json")[1]
            before = tmp / f"before{target}"
            shutil.copytree(template, before)
            if target == 12:
                assert engine("ingest", "--input", batch[11], "--state", before).returncode == 0
            ref[target, "prev-uncommitted"] = snapshot(before, queries[target - 1], tmp / "ref.json")[1]
        print(f"clean ingest duration: batch 11 {duration[11]:.2f}s, batch 12 {duration[12]:.2f}s")

        outcomes = Counter()
        for trial in range(args.trials):
            target = 11 if trial % 2 == 0 else 12
            state = tmp / f"t{trial}"
            shutil.copytree(template, state)
            if target == 12:
                assert engine("ingest", "--input", batch[11], "--state", state).returncode == 0
            delay = rng.uniform(0, duration[target] * 1.1)
            p = subprocess.Popen(CMD + ["ingest", "--input", str(batch[target]), "--state", str(state)],
                                 env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(delay)
            finished = p.poll() is not None
            if not finished:
                hard_kill(p)

            conn = sqlite3.connect(state / "engine.db")
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                committed = conn.execute("SELECT COUNT(*) FROM batches WHERE batch = ?",
                                         (target,)).fetchone()[0] == 1
                staged = conn.execute("SELECT COUNT(*) FROM ledger_rev WHERE batch = ?",
                                      (target,)).fetchone()[0]
            finally:
                conn.close()
            problems = []
            if integrity != "ok":
                problems.append(f"integrity_check: {integrity}")
            prev = ref[target, target - 1] if committed else ref[target, "prev-uncommitted"]
            if snapshot(state, queries[target - 1], tmp / "o.json") != (0, prev):
                problems.append("previous-cutoff snapshot not equal to the pre- or post-commit reference")
            code, body = snapshot(state, queries[target], tmp / "o.json")
            if committed and (code, body) != (0, ref[target, target]):
                problems.append("committed but snapshot differs from reference")
            if not committed and code != 65:
                problems.append(f"uncommitted but snapshot exit {code}")
            if engine("ingest", "--input", batch[target], "--state", state).returncode != 0:
                problems.append("replay failed")
            if snapshot(state, queries[target], tmp / "o.json") != (0, ref[target, target]):
                problems.append("after replay, snapshot differs from reference")

            phase = ("finished" if finished else "killed after commit" if committed
                     else "killed after staging" if staged else "killed before staging")
            outcomes[phase] += 1
            status = "ok" if not problems else "VIOLATION: " + "; ".join(problems)
            print(f"trial {trial:2d} batch {target} delay {delay:.2f}s {phase:22s} {status}")
            shutil.rmtree(state, ignore_errors=True)
            if problems:
                sys.exit(1)
        print(f"\n{args.trials} trials, no violations. outcomes: {dict(outcomes)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
