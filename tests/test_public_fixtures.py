"""Public fixture parity for the pure core (no storage involved).

Public expected files assume all batches are ingested, so erasures from
every batch apply to every query.
"""
import json
import random
import unittest
from pathlib import Path

from engine.core import evaluate, parse_query
from engine.records import parse_line

ROOT = Path(__file__).resolve().parent.parent


def load_rows():
    rows = []
    for path in sorted((ROOT / "data").glob("batch_*.jsonl")):
        with path.open(encoding="utf-8") as f:
            rows.extend(parse_line(line) for line in f if line.strip())
    return rows


def load_cases():
    for qpath in sorted((ROOT / "queries").glob("*.json")):
        query = parse_query(json.loads(qpath.read_text(encoding="utf-8")))
        expected = json.loads((ROOT / "expected" / qpath.name).read_text(encoding="utf-8"))
        yield query, expected


class PublicFixtureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = load_rows()
        cls.cases = list(load_cases())

    def test_cases_present(self):
        self.assertEqual(len(self.cases), 10)

    def test_every_public_query_matches_exactly(self):
        for query, expected in self.cases:
            with self.subTest(query=query.query_id):
                self.assertEqual(evaluate(self.rows, query), expected)

    def test_row_order_does_not_matter(self):
        rng = random.Random(7)
        for _ in range(10):
            shuffled = self.rows[:]
            rng.shuffle(shuffled)
            for query, expected in self.cases:
                self.assertEqual(evaluate(shuffled, query), expected, query.query_id)

    def test_exact_retry_does_not_change_results(self):
        doubled = self.rows + self.rows
        for query, expected in self.cases:
            self.assertEqual(evaluate(doubled, query), expected, query.query_id)


if __name__ == "__main__":
    unittest.main()
