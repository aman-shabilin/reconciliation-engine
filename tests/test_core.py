"""Scenario tests for selection, conflicts, joins and output (spec §3–4)."""
import itertools
import unittest

from engine.core import Query, evaluate
from engine.records import (Erasure, FxRev, LedgerRev, MerchantRev, Row,
                            Unsupported, parse_row)

_ids = itertools.count()


def row(batch, record, row_id=None):
    return Row(batch, row_id or f"r{next(_ids)}", record)


def L(entry_id, version, amount, *, op="POST", merchant="M", token="C1",
      at="2026-01-01T12:00:00Z", currency="USD"):
    return LedgerRev(entry_id, version, op, merchant, token, at, currency, amount)


def M(merchant="M", valid_from="2026-01-01T00:00:00Z",
      valid_to="2026-02-01T00:00:00Z", version=1, region="EU", tier="std"):
    return MerchantRev(merchant, valid_from, version, valid_to, region, tier)


def F(currency="USD", date="2026-01-01", num=1, den=1, version=1):
    return FxRev(currency, date, version, num, den)


def Q(cutoff, start="2026-01-01", end="2026-01-05"):
    return Query("q", cutoff, start, end)


def base(batch=1, **fx):
    """One merchant M valid all January, and a USD rate for Jan 1-4."""
    rows = [row(batch, M())]
    for d in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"):
        rows.append(row(batch, F(date=d, **fx)))
    return rows


def exc(report):
    return [(e["stream"], e["key"], e["code"], e["version"]) for e in report["exceptions"]]


def usd(report):
    return {t["entry_id"]: t["usd_micro"] for t in report["trace"]}


class WorkedExamplesTest(unittest.TestCase):
    def test_1_round_per_entry_then_sum_zero_net_group_kept(self):
        rows = base(num=1, den=2) + [
            row(2, L(f"E{a}", 1, a)) for a in (1, 3, -1, -3)]
        r = evaluate(rows, Q(2))
        self.assertEqual(r["settlements"], [{
            "date": "2026-01-01", "merchant_id": "M", "region": "EU",
            "tier": "std", "net_usd_micro": 0, "entry_count": 4}])
        self.assertEqual(usd(r), {"E1": 0, "E3": 2, "E-1": 0, "E-3": -2})

    def test_2_revisions_replace_and_void_removes(self):
        rows = base(num=2, den=1) + [
            row(2, L("A", 1, 100)),
            row(3, L("A", 2, 70)),
            row(4, L("A", 3, 100, op="VOID")),
        ]
        self.assertEqual(usd(evaluate(rows, Q(2))), {"A": 200})
        self.assertEqual(usd(evaluate(rows, Q(3))), {"A": 140})
        r = evaluate(rows, Q(4))
        self.assertEqual((r["settlements"], r["trace"], r["exceptions"]), ([], [], []))

    def test_3_conflict_blocks_until_higher_version(self):
        rows = base() + [
            row(2, L("A", 1, 10)),
            row(3, L("A", 2, 20)),
            row(3, L("A", 2, 21)),
        ]
        r = evaluate(rows, Q(3))
        self.assertEqual(r["trace"], [])
        self.assertEqual(exc(r), [("ledger", "A", "REVISION_CONFLICT", 2)])

        rows.append(row(4, L("A", 1, 10)))  # seeing v1 again does not restore A
        self.assertEqual(evaluate(rows, Q(4))["trace"], [])

        rows.append(row(5, L("A", 3, 30)))
        r = evaluate(rows, Q(5))
        self.assertEqual(usd(r), {"A": 30})
        self.assertEqual(exc(r), [])

        # A repeat v3 delivered in ledger schema 2 is a duplicate, not a conflict.
        v2_payload = {"entry_id": "A", "version": 3, "op": "POST", "merchant_id": "M",
                      "customer_token": "C1", "effective_at": "2026-01-01T12:00:00Z",
                      "currency": "USD", "amount_minor_text": "30"}
        rows.append(parse_row({"batch": 6, "row_id": "rv2", "stream": "ledger",
                               "schema_version": 2, "payload": v2_payload}))
        r = evaluate(rows, Q(6))
        self.assertEqual(usd(r), {"A": 30})
        self.assertEqual(exc(r), [])

    def test_4_interval_end_is_exclusive(self):
        rows = [row(1, M(valid_to="2026-01-02T00:00:00Z")),
                row(1, F(date="2026-01-02")),
                row(2, L("A", 1, 5, at="2026-01-02T00:00:00Z")),
                row(2, L("B", 1, 5, at="2026-01-01T23:59:59Z")),
                row(1, F(date="2026-01-01"))]
        r = evaluate(rows, Q(2))
        self.assertEqual(usd(r), {"B": 5})
        self.assertEqual(exc(r), [("ledger", "A", "MISSING_MERCHANT", 1)])

    def test_4_interval_start_is_inclusive(self):
        rows = [row(1, M(valid_from="2026-01-02T00:00:00Z")),
                row(1, F(date="2026-01-02")),
                row(2, L("A", 1, 5, at="2026-01-02T00:00:00Z"))]
        self.assertEqual(usd(evaluate(rows, Q(2))), {"A": 5})

    def test_4_ambiguous_even_when_attributes_match(self):
        rows = base() + [
            row(1, M(valid_from="2026-01-01T06:00:00Z")),  # same region/tier, overlaps
            row(2, L("A", 1, 5)),
        ]
        r = evaluate(rows, Q(2))
        self.assertEqual(r["trace"], [])
        self.assertEqual(exc(r), [("ledger", "A", "AMBIGUOUS_MERCHANT", 1)])

    def test_5_changed_effective_date_moves_between_days(self):
        rows = base() + [
            row(2, L("A", 1, 5, at="2026-01-01T12:00:00Z")),
            row(3, L("A", 2, 5, at="2026-01-02T12:00:00Z")),
        ]
        day1, day2 = ("2026-01-01", "2026-01-02"), ("2026-01-02", "2026-01-03")
        self.assertEqual(usd(evaluate(rows, Q(2, *day1))), {"A": 5})
        self.assertEqual(usd(evaluate(rows, Q(3, *day1))), {})
        self.assertEqual(usd(evaluate(rows, Q(3, *day2))), {"A": 5})

    def test_6_future_fx_does_not_change_old_cutoff_but_erasure_masks_it(self):
        rows = base(num=1, den=1) + [
            row(2, L("A", 1, 10, token="C9")),
            row(3, F(date="2026-01-01", num=5, den=1, version=2)),
            row(4, Erasure("C9")),
        ]
        old = evaluate(rows, Q(2))
        new = evaluate(rows, Q(3))
        self.assertEqual(usd(old), {"A": 10})
        self.assertEqual(usd(new), {"A": 50})
        self.assertIsNone(old["trace"][0]["customer_token"])
        self.assertIsNone(new["trace"][0]["customer_token"])

    def test_7_large_integer_exact(self):
        amount = 9_007_199_254_740_993
        rows = base() + [row(2, L("A", 1, amount))]
        r = evaluate(rows, Q(2))
        self.assertEqual(r["settlements"][0]["net_usd_micro"], amount)
        self.assertEqual(r["trace"][0]["usd_micro"], amount)


class SelectionTest(unittest.TestCase):
    def test_highest_version_wins_not_last_arriving(self):
        rows = base() + [row(2, L("A", 2, 20)), row(3, L("A", 1, 10))]
        self.assertEqual(usd(evaluate(rows, Q(3))), {"A": 20})

    def test_identical_copies_collapse(self):
        rows = base() + [row(2, L("A", 1, 10)), row(3, L("A", 1, 10))]
        r = evaluate(rows, Q(3))
        self.assertEqual(usd(r), {"A": 10})
        self.assertEqual(r["settlements"][0]["entry_count"], 1)

    def test_conflict_with_void_variant_is_still_conflict(self):
        rows = base() + [row(2, L("A", 1, 10)), row(2, L("A", 1, 10, op="VOID"))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [("ledger", "A", "REVISION_CONFLICT", 1)])

    def test_three_variants_emit_one_conflict(self):
        rows = base() + [row(2, L("A", 1, n)) for n in (1, 2, 3)]
        self.assertEqual(exc(evaluate(rows, Q(2))), [("ledger", "A", "REVISION_CONFLICT", 1)])

    def test_lower_version_conflict_not_reported(self):
        rows = base() + [row(2, L("A", 1, 1)), row(2, L("A", 1, 2)), row(3, L("A", 2, 3))]
        self.assertEqual(exc(evaluate(rows, Q(3))), [])

    def test_ledger_conflict_outside_date_range_reported(self):
        rows = base() + [row(2, L("A", 1, 1, at="2026-03-01T00:00:00Z")),
                         row(2, L("A", 1, 2, at="2026-03-01T00:00:00Z"))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [("ledger", "A", "REVISION_CONFLICT", 1)])

    def test_unreferenced_merchant_and_fx_conflicts_reported(self):
        rows = [row(1, M("X", region="A")), row(1, M("X", region="B")),
                row(1, F("XTS", num=1)), row(1, F("XTS", num=2))]
        self.assertEqual(exc(evaluate(rows, Q(1))), [
            ("fx", "XTS|2026-01-01", "REVISION_CONFLICT", 1),
            ("merchants", "X|2026-01-01T00:00:00Z", "REVISION_CONFLICT", 1),
        ])

    def test_merchant_revision_replaces_interval_end(self):
        # v1 [Jan1, Feb1) + second interval from Jan2 is ambiguous; v2 shortens v1.
        rows = base() + [
            row(1, M(valid_from="2026-01-02T00:00:00Z", region="US")),
            row(2, L("A", 1, 5, at="2026-01-02T12:00:00Z")),
        ]
        self.assertEqual(exc(evaluate(rows, Q(2)))[0][2], "AMBIGUOUS_MERCHANT")
        rows.append(row(3, M(valid_to="2026-01-02T00:00:00Z", version=2)))
        r = evaluate(rows, Q(3))
        self.assertEqual(r["trace"][0]["region"], "US")
        self.assertEqual(exc(r), [])

    def test_revision_may_change_merchant_and_currency(self):
        rows = base() + [
            row(1, M("N", region="SG")),
            row(1, F("EUR", num=3)),
            row(2, L("A", 1, 10)),
            row(3, L("A", 2, 10, merchant="N", currency="EUR")),
        ]
        t = evaluate(rows, Q(3))["trace"][0]
        self.assertEqual((t["merchant_id"], t["region"], t["usd_micro"], t["version"]),
                         ("N", "SG", 30, 2))


class JoinTest(unittest.TestCase):
    def test_blocked_merchant_is_absent_for_joins(self):
        rows = [row(1, M(region="A")), row(1, M(region="B")), row(1, F()),
                row(2, L("A", 1, 5))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [
            ("ledger", "A", "MISSING_MERCHANT", 1),
            ("merchants", "M|2026-01-01T00:00:00Z", "REVISION_CONFLICT", 1),
        ])

    def test_blocked_fx_is_absent_for_joins(self):
        rows = [row(1, M()), row(1, F(num=1)), row(1, F(num=2)), row(2, L("A", 1, 5))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [
            ("fx", "USD|2026-01-01", "REVISION_CONFLICT", 1),
            ("ledger", "A", "MISSING_FX", 1),
        ])

    def test_only_first_join_error_missing_merchant_before_fx(self):
        rows = [row(2, L("A", 1, 5, merchant="nobody", currency="ZZZ"))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [("ledger", "A", "MISSING_MERCHANT", 1)])

    def test_only_first_join_error_ambiguous_before_fx(self):
        rows = [row(1, M()), row(1, M(valid_from="2026-01-01T06:00:00Z")),
                row(2, L("A", 1, 5, currency="ZZZ"))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [("ledger", "A", "AMBIGUOUS_MERCHANT", 1)])

    def test_usd_has_no_implicit_rate(self):
        rows = [row(1, M()), row(2, L("A", 1, 5))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [("ledger", "A", "MISSING_FX", 1)])

    def test_join_error_carries_selected_version(self):
        rows = [row(1, M()), row(2, L("A", 1, 5)), row(3, L("A", 4, 5, currency="ZZZ"))]
        self.assertEqual(exc(evaluate(rows, Q(3))), [("ledger", "A", "MISSING_FX", 4)])

    def test_join_errors_only_in_date_range(self):
        rows = [row(2, L("A", 1, 5, at="2026-01-05T00:00:00Z"))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [])

    def test_void_produces_no_join_error(self):
        rows = [row(2, L("A", 1, 5, op="VOID", merchant="nobody"))]
        self.assertEqual(exc(evaluate(rows, Q(2))), [])

    def test_fx_uses_utc_date_of_entry(self):
        rows = [row(1, M()), row(1, F(date="2026-01-01", num=1)),
                row(1, F(date="2026-01-02", num=7)),
                row(2, L("A", 1, 1, at="2026-01-01T23:59:59Z")),
                row(2, L("B", 1, 1, at="2026-01-02T00:00:00Z"))]
        self.assertEqual(usd(evaluate(rows, Q(2))), {"A": 1, "B": 7})

    def test_date_range_start_inclusive_end_exclusive(self):
        rows = base() + [row(2, L("A", 1, 1, at="2026-01-02T00:00:00Z")),
                         row(2, L("B", 1, 1, at="2026-01-03T00:00:00Z"))]
        self.assertEqual(usd(evaluate(rows, Q(2, "2026-01-02", "2026-01-03"))), {"A": 1})

    def test_empty_range(self):
        r = evaluate(base() + [row(2, L("A", 1, 1))], Q(2, "2027-01-01", "2027-01-02"))
        self.assertEqual(r, {"query_id": "q", "settlements": [], "trace": [], "exceptions": []})


class KnowledgeTimeTest(unittest.TestCase):
    def test_later_batches_invisible_at_earlier_cutoff(self):
        rows = base() + [row(2, L("A", 1, 1)), row(3, L("B", 1, 1)),
                         row(3, Unsupported("u1", "x"))]
        r = evaluate(rows, Q(2))
        self.assertEqual(usd(r), {"A": 1})
        self.assertEqual(exc(r), [])

    def test_unsupported_reported_once_regardless_of_date(self):
        rows = [row(1, Unsupported("u1", "future"), row_id="u1"),
                row(1, Unsupported("u1", "future"), row_id="u1")]
        self.assertEqual(exc(evaluate(rows, Q(1, "2030-01-01", "2030-01-02"))),
                         [("future", "u1", "UNSUPPORTED_SCHEMA", None)])

    def test_erasure_masks_token_only(self):
        rows = base() + [row(2, L("A", 1, 1, token="C1")),
                         row(2, L("B", 1, 1, token="C2")),
                         row(3, Erasure("C1"))]
        r = evaluate(rows, Q(2))
        self.assertEqual({t["entry_id"]: t["customer_token"] for t in r["trace"]},
                         {"A": None, "B": "C2"})
        self.assertEqual(r["settlements"][0]["net_usd_micro"], 2)


class OutputShapeTest(unittest.TestCase):
    def test_sorting_is_by_string_and_null_version_first(self):
        rows = base() + [
            row(1, M("M", valid_from="2026-02-01T00:00:00Z", valid_to="2026-03-01T00:00:00Z",
                     region="A")),
            row(1, M("M", valid_from="2026-02-01T00:00:00Z", valid_to="2026-03-01T00:00:00Z",
                     region="B")),
            row(2, L("E10", 1, 1)), row(2, L("E2", 1, 1)),
            row(2, Unsupported("M|2026-02-01T00:00:00Z", "merchants")),
        ]
        r = evaluate(rows, Q(2))
        self.assertEqual([t["entry_id"] for t in r["trace"]], ["E10", "E2"])
        self.assertEqual(exc(r), [
            ("merchants", "M|2026-02-01T00:00:00Z", "REVISION_CONFLICT", 1),
            ("merchants", "M|2026-02-01T00:00:00Z", "UNSUPPORTED_SCHEMA", None),
        ])

    def test_exception_sort_key_puts_null_before_integers(self):
        # Through a report, exceptions sharing (stream, key, code) never differ
        # only in version, so the sort key is exercised directly.
        from engine.core import _exception_sort_key
        items = [("s", "k", "c", 1), ("s", "k", "c", None), ("s", "k", "c", 0)]
        self.assertEqual(sorted(items, key=_exception_sort_key),
                         [("s", "k", "c", None), ("s", "k", "c", 0), ("s", "k", "c", 1)])

    def test_groups_split_by_region_and_sorted(self):
        rows = [row(1, M("M", valid_to="2026-01-01T12:00:00Z", region="Z")),
                row(1, M("M", valid_from="2026-01-01T12:00:00Z", region="A")),
                row(1, M("K", region="Q")),
                row(1, F()),
                row(2, L("A", 1, 1, at="2026-01-01T06:00:00Z")),
                row(2, L("B", 1, 2, at="2026-01-01T18:00:00Z")),
                row(2, L("C", 1, 4, merchant="K"))]
        keys = [(s["merchant_id"], s["region"], s["net_usd_micro"])
                for s in evaluate(rows, Q(2))["settlements"]]
        self.assertEqual(keys, [("K", "Q", 4), ("M", "A", 2), ("M", "Z", 1)])

    def test_exact_top_level_fields_and_int_types(self):
        r = evaluate(base() + [row(2, L("A", 1, 3))], Q(2))
        self.assertEqual(set(r), {"query_id", "settlements", "trace", "exceptions"})
        for k in ("net_usd_micro", "entry_count"):
            self.assertIs(type(r["settlements"][0][k]), int)
        for k in ("usd_micro", "version"):
            self.assertIs(type(r["trace"][0][k]), int)


if __name__ == "__main__":
    unittest.main()
