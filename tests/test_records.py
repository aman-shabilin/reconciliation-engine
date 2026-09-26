import unittest

from engine.records import (Erasure, LedgerRev, Unsupported, parse_int_text,
                            parse_row)

LEDGER_V1 = {
    "entry_id": "E1", "version": 3, "op": "POST", "merchant_id": "M1",
    "customer_token": "C1", "effective_at": "2026-01-01T12:00:00Z",
    "currency": "USD", "amount_minor": 99,
}


def envelope(stream, schema_version, payload, row_id="r1", batch=1, **extra):
    return {"batch": batch, "row_id": row_id, "stream": stream,
            "schema_version": schema_version, "payload": payload, **extra}


def ledger_v2(amount_text, **overrides):
    p = {k: v for k, v in LEDGER_V1.items() if k != "amount_minor"}
    p["amount_minor_text"] = amount_text
    p.update(overrides)
    return p


class LedgerNormalisationTest(unittest.TestCase):
    def test_v1_and_v2_equal_payloads_are_business_equal(self):
        a = parse_row(envelope("ledger", 1, LEDGER_V1, row_id="a", batch=5)).record
        b = parse_row(envelope("ledger", 2, ledger_v2("99"), row_id="b", batch=6)).record
        self.assertIsInstance(a, LedgerRev)
        self.assertEqual(a, b)
        self.assertEqual(len({a, b}), 1)

    def test_v2_amount_parsed_as_exact_int(self):
        r = parse_row(envelope("ledger", 2, ledger_v2("-9007199254740993"))).record
        self.assertEqual(r.amount_minor, -9007199254740993)
        self.assertIs(type(r.amount_minor), int)

    def test_v2_different_amount_is_different(self):
        a = parse_row(envelope("ledger", 1, LEDGER_V1)).record
        b = parse_row(envelope("ledger", 2, ledger_v2("98"))).record
        self.assertNotEqual(a, b)

    def test_extra_payload_and_envelope_fields_ignored(self):
        a = parse_row(envelope("ledger", 1, LEDGER_V1)).record
        b = parse_row(envelope("ledger", 1, {**LEDGER_V1, "note": "x"},
                               row_id="other", batch=9, trace_id="t")).record
        self.assertEqual(a, b)


class IntTextTest(unittest.TestCase):
    def test_accepts_signed_decimal(self):
        self.assertEqual(parse_int_text("+5"), 5)
        self.assertEqual(parse_int_text("-007"), -7)
        self.assertEqual(parse_int_text("0"), 0)

    def test_rejects_non_canonical_forms(self):
        for bad in ["", " 5", "5 ", "1_000", "1.0", "1e3", "--1", "0x10", "٣"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_int_text(bad)


class SchemaRoutingTest(unittest.TestCase):
    def assertUnsupported(self, obj):
        r = parse_row(obj).record
        self.assertEqual(r, Unsupported(obj["row_id"], obj["stream"]))

    def test_unknown_version_is_unsupported(self):
        self.assertUnsupported(envelope("ledger", 99, LEDGER_V1))
        self.assertUnsupported(envelope("merchants", 2, {}))
        self.assertUnsupported(envelope("profiles", 2, {"name": "N", "email": "e"}))

    def test_unknown_stream_is_unsupported(self):
        self.assertUnsupported(envelope("future_stream", 1, {"note": "ignore"}))

    def test_boolean_is_not_version_1(self):
        self.assertUnsupported(envelope("ledger", True, LEDGER_V1))

    def test_unsupported_keeps_no_payload(self):
        r = parse_row(envelope("mystery", 1, {"name": "SECRET_NAME"})).record
        self.assertNotIn("SECRET_NAME", repr(r))

    def test_profile_is_discarded(self):
        row = parse_row(envelope("profiles", 1, {"customer_token": "C1",
                                                 "name": "SECRET_NAME",
                                                 "email": "secret@example.invalid"}))
        self.assertIsNone(row.record)
        self.assertNotIn("SECRET", repr(row))
        self.assertNotIn("example.invalid", repr(row))

    def test_erasure(self):
        r = parse_row(envelope("erasures", 1, {"customer_token": "C1"})).record
        self.assertEqual(r, Erasure("C1"))


if __name__ == "__main__":
    unittest.main()
