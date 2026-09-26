"""Durability, recovery, isolation and privacy of the storage layer (spec §5).

Two styles:
  * CLI tests run the real process, so failpoints really os._exit(86).
  * In-process tests swap the crash for an exception to go fast; closing a
    connection with an open transaction discards it, like a crash would.
The in-memory `evaluate` is the oracle for storage-backed results.
"""
import hashlib
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from engine import store
from engine.core import evaluate, parse_query
from engine.records import parse_line

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BATCHES = {int(p.stem.split("_")[1]): p for p in sorted(DATA.glob("batch_*.jsonl"))}
QUERIES = {p.stem: parse_query(json.loads(p.read_text(encoding="utf-8")))
           for p in sorted((ROOT / "queries").glob("*.json"))}
EXPECTED = {name: json.loads((ROOT / "expected" / f"{name}.json").read_text(encoding="utf-8"))
            for name in QUERIES}
ROWS = {b: [parse_line(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        for b, p in BATCHES.items()}
PII_MARKERS = (b"SYNTHETIC", b"example.invalid", b"SECRET_PII")


class Crashed(Exception):
    pass


def crash(code):
    raise Crashed(code)


def quiet(_msg):
    pass


def oracle(committed, query):
    return evaluate([r for b in committed for r in ROWS[b]], query)


def state_bytes(state: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(state.iterdir()) if p.is_file())


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="recon-test-"))
        self.state = self.tmp / "state"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ingest(self, batch, failpoint=None):
        return store.ingest(BATCHES[batch], self.state, failpoint, crash=crash, log=quiet)

    def commit_all(self, batches):
        for b in batches:
            self.assertEqual(self.ingest(b), "committed")

    def report(self, name, **kw):
        return store.load_report(self.state, QUERIES[name], **kw)

    def counts(self):
        conn = sqlite3.connect(self.state / store.DB_NAME)
        try:
            return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("batches", "ledger_rev", "merchant_rev", "fx_rev",
                              "unsupported", "erasures")}
        finally:
            conn.close()


class FailpointRecoveryTest(TempDirTest):
    def _check_failpoint(self, failpoint):
        # Batch 10 carries the erasures of C0/C1: a staged erasure must not mask.
        self.commit_all(range(1, 10))
        before = {n: self.report(n) for n, q in QUERIES.items() if q.cutoff_batch <= 9}
        counts_before = self.counts()

        with self.assertRaises(Crashed) as cm:
            self.ingest(10, failpoint)
        self.assertEqual(cm.exception.args, (86,))

        for name, query in QUERIES.items():
            if query.cutoff_batch <= 9:
                self.assertEqual(self.report(name), before[name], name)
                self.assertEqual(self.report(name), oracle(range(1, 10), query), name)
            else:
                with self.assertRaises(store.GapError, msg=name):
                    self.report(name)
        tokens = {t["customer_token"] for t in self.report("q09")["trace"]}
        self.assertIn("C0", tokens, "uncommitted erasure must not mask")
        self.assertEqual(self.counts()["batches"], counts_before["batches"])

        self.assertEqual(self.ingest(10), "committed")
        after_first = self.counts()
        self.assertEqual(self.ingest(10), "already-committed")
        self.assertEqual(self.ingest(10, failpoint), "already-committed")
        self.assertEqual(self.counts(), after_first, "replay must commit exactly once")
        tokens = {t["customer_token"] for t in self.report("q09")["trace"]}
        self.assertNotIn("C0", tokens)

    def test_after_stage(self):
        self._check_failpoint("after-stage")

    def test_before_commit(self):
        self._check_failpoint("before-commit")

    def test_after_stage_really_stages_durably(self):
        self.commit_all(range(1, 3))
        before = self.counts()
        with self.assertRaises(Crashed):
            self.ingest(3, "after-stage")
        staged = self.counts()
        self.assertEqual(staged["batches"], before["batches"])
        self.assertEqual(staged["ledger_rev"], before["ledger_rev"] + 10)
        self.assertEqual(staged["unsupported"], before["unsupported"] + 1)

    def test_before_commit_leaves_nothing_new(self):
        self.commit_all(range(1, 3))
        before = self.counts()
        with self.assertRaises(Crashed):
            self.ingest(3, "before-commit")
        # Staging (transaction A) is durable; only the checkpoint was lost.
        self.assertEqual(self.counts()["batches"], before["batches"])

    def test_orphan_staged_batch_never_leaks(self):
        self.commit_all(range(1, 5))
        with self.assertRaises(Crashed):
            self.ingest(5, "after-stage")
        self.assertEqual(self.report("q04"), oracle(range(1, 5), QUERIES["q04"]))


class BatchIdentityTest(TempDirTest):
    def write_batch(self, name, lines):
        path = self.tmp / name
        path.write_text("".join(l if l.endswith("\n") else l + "\n" for l in lines),
                        encoding="utf-8")
        return path

    def batch_lines(self, batch):
        return BATCHES[batch].read_text(encoding="utf-8").splitlines()

    def rogue_ledger_line(self, batch):
        return json.dumps({
            "batch": batch, "row_id": "rogue-1", "stream": "ledger", "schema_version": 1,
            "payload": {"entry_id": "ROGUE", "version": 1, "op": "POST",
                        "merchant_id": "K17_M0", "customer_token": "CX",
                        "effective_at": "2026-01-01T12:00:00Z", "currency": "USD",
                        "amount_minor": 1000}})

    def test_reordered_retry_is_the_same_batch(self):
        self.commit_all([1, 2, 3])
        before = self.counts()
        reordered = self.write_batch("b3.jsonl", list(reversed(self.batch_lines(3))) + [""])
        self.assertEqual(store.ingest(reordered, self.state, crash=crash, log=quiet),
                         "already-committed")
        self.assertEqual(self.counts(), before)

    def test_different_file_for_committed_batch_is_rejected_without_change(self):
        self.commit_all([1, 2, 3])
        before = self.counts()
        altered = self.write_batch("b3.jsonl", self.batch_lines(3) + [self.rogue_ledger_line(3)])
        with self.assertRaises(store.IntegrityError):
            store.ingest(altered, self.state, crash=crash, log=quiet)
        self.assertEqual(self.counts(), before)
        r = cli("ingest", "--input", altered, "--state", self.state, cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already committed with a different row set", r.stderr)

    def test_orphan_rows_from_a_different_staged_file_are_not_published(self):
        self.commit_all([1, 2])
        wrong = self.write_batch("wrong3.jsonl", self.batch_lines(3) + [self.rogue_ledger_line(3)])
        with self.assertRaises(Crashed):
            store.ingest(wrong, self.state, "after-stage", crash=crash, log=quiet)
        self.assertEqual(self.ingest(3), "committed")
        self.assertEqual(self.report("q03"), oracle([1, 2, 3], QUERIES["q03"]))

    def test_checkpoint_holds_no_payload_derived_fingerprint(self):
        self.commit_all([1])
        conn = sqlite3.connect(self.state / store.DB_NAME)
        try:
            (fp,) = conn.execute("SELECT row_set_sha256 FROM batches").fetchone()
        finally:
            conn.close()
        self.assertEqual(fp, store.row_set_fingerprint(ROWS[1]))
        self.assertNotEqual(fp, hashlib.sha256(BATCHES[1].read_bytes()).hexdigest())


class GapAndOrderTest(TempDirTest):
    def test_gap_rejected(self):
        self.commit_all([1, 2, 4])
        self.assertEqual(self.report("q02"), oracle([1, 2], QUERIES["q02"]))
        for name in ("q03", "q04", "q12"):
            with self.assertRaises(store.GapError):
                self.report(name)

    def test_empty_state_is_a_gap_and_is_not_created(self):
        with self.assertRaises(store.GapError):
            self.report("q01")
        self.assertFalse(self.state.exists())

    def test_random_orders_with_retries_match_oracle_at_every_step(self):
        rng = random.Random(20260926)
        for trial in range(20):
            state = self.tmp / f"s{trial}"
            order = list(BATCHES)
            rng.shuffle(order)
            order += rng.sample(order, 4)  # exact retries of whole files
            committed = set()
            for b in order:
                store.ingest(BATCHES[b], state, crash=crash, log=quiet)
                committed.add(b)
                for name, q in QUERIES.items():
                    if set(range(1, q.cutoff_batch + 1)) <= committed:
                        got = store.load_report(state, q)
                        self.assertEqual(got, oracle(sorted(committed), q),
                                         f"trial {trial} order {order} query {name}")
                    else:
                        with self.assertRaises(store.GapError):
                            store.load_report(state, q)
            for name, q in QUERIES.items():
                self.assertEqual(store.load_report(state, q), EXPECTED[name], name)


class IsolationTest(TempDirTest):
    def test_reader_keeps_its_snapshot_while_erasure_commits(self):
        self.commit_all(range(1, 10))
        writer_ran = []

        def concurrent_writer():
            store.ingest(BATCHES[10], self.state, crash=crash, log=quiet)
            writer_ran.append(True)

        during = self.report("q09", _after_gap_check=concurrent_writer)
        self.assertEqual(writer_ran, [True])
        self.assertEqual(during, oracle(range(1, 10), QUERIES["q09"]))
        self.assertIn("C0", {t["customer_token"] for t in during["trace"]})
        after = self.report("q09")
        self.assertNotIn("C0", {t["customer_token"] for t in after["trace"]})


class PrivacyTest(TempDirTest):
    def test_no_pii_in_state_after_all_batches_including_replayed_profile(self):
        self.commit_all(sorted(BATCHES))
        blob = state_bytes(self.state)
        for marker in PII_MARKERS:
            self.assertNotIn(marker, blob)

    def test_unknown_schema_payload_never_persisted(self):
        path = self.tmp / "batch.jsonl"
        lines = [
            {"batch": 1, "row_id": "x1", "stream": "mystery", "schema_version": 1,
             "payload": {"name": "SECRET_PII_NAME", "email": "a@SECRET_PII.test"}},
            {"batch": 1, "row_id": "x2", "stream": "profiles", "schema_version": 7,
             "payload": {"customer_token": "C1", "name": "SECRET_PII_NAME2"}},
            {"batch": 1, "row_id": "x3", "stream": "profiles", "schema_version": 1,
             "payload": {"customer_token": "C1", "name": "SECRET_PII_NAME3",
                         "email": "b@SECRET_PII.test"}},
        ]
        path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")
        store.ingest(path, self.state, crash=crash, log=quiet)
        self.assertNotIn(b"SECRET_PII", state_bytes(self.state))
        q = parse_query({"query_id": "u", "cutoff_batch": 1,
                         "start_date": "2026-01-01", "end_date": "2026-01-02"})
        codes = [(e["stream"], e["key"]) for e in store.load_report(self.state, q)["exceptions"]]
        self.assertEqual(codes, [("mystery", "x1"), ("profiles", "x2")])


def cli(*args, cwd):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, "-m", "engine.cli", *map(str, args)],
                          cwd=cwd, env=env, capture_output=True, text=True)


class CliTest(TempDirTest):
    """Real processes, run from an unrelated working directory."""

    def run_cli(self, *args):
        return cli(*args, cwd=self.tmp)

    def test_public_fixtures_through_cli(self):
        for b in sorted(BATCHES):
            r = self.run_cli("ingest", "--input", BATCHES[b], "--state", self.state)
            self.assertEqual(r.returncode, 0, r.stderr)
        for name in QUERIES:
            out = self.tmp / f"{name}.json"
            r = self.run_cli("snapshot", "--query", ROOT / "queries" / f"{name}.json",
                             "--state", self.state, "--out", out)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), EXPECTED[name], name)

    def test_real_failpoints_exit_86_then_replay_commits_once(self):
        for fp in store.FAILPOINTS:
            state = self.tmp / fp
            for b in (1, 2):
                self.assertEqual(self.run_cli("ingest", "--input", BATCHES[b],
                                              "--state", state).returncode, 0)
            r = self.run_cli("ingest", "--input", BATCHES[3], "--state", state, "--failpoint", fp)
            self.assertEqual(r.returncode, 86, r.stderr)
            out = self.tmp / f"{fp}.json"
            q03 = ROOT / "queries" / "q03.json"
            self.assertEqual(self.run_cli("snapshot", "--query", q03, "--state", state,
                                          "--out", out).returncode, 65)
            self.assertFalse(out.exists())
            self.assertEqual(self.run_cli("ingest", "--input", BATCHES[3],
                                          "--state", state).returncode, 0)
            self.assertEqual(self.run_cli("ingest", "--input", BATCHES[3], "--state", state,
                                          "--failpoint", fp).returncode, 0)
            self.assertEqual(self.run_cli("snapshot", "--query", q03, "--state", state,
                                          "--out", out).returncode, 0)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             oracle([1, 2, 3], QUERIES["q03"]))

    def test_gap_preserves_existing_output_byte_for_byte(self):
        for b in (1, 2, 4):
            self.run_cli("ingest", "--input", BATCHES[b], "--state", self.state)
        out = self.tmp / "result.json"
        out.write_bytes(b"previous result \x00\xff bytes")
        digest = hashlib.sha256(out.read_bytes()).hexdigest()
        r = self.run_cli("snapshot", "--query", ROOT / "queries" / "q04.json",
                         "--state", self.state, "--out", out)
        self.assertEqual(r.returncode, 65)
        self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), digest)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith(".")], [],
                         "no temp files left behind")

    def test_stderr_has_no_pii_or_tokens(self):
        errs = ""
        for b in sorted(BATCHES):
            errs += self.run_cli("ingest", "--input", BATCHES[b], "--state", self.state).stderr
        errs += self.run_cli("snapshot", "--query", ROOT / "queries" / "q12.json",
                             "--state", self.state, "--out", self.tmp / "o.json").stderr
        for marker in ("SYNTHETIC", "example.invalid", "C0", "C1"):
            self.assertNotIn(marker, errs)

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "needs POSIX sh")
    def test_run_adapter(self):
        r = subprocess.run([str(ROOT / "run"), "ingest", "--input", str(BATCHES[1]),
                            "--state", str(self.state)], cwd=self.tmp,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
