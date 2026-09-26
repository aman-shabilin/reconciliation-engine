import json
import random
import unittest
from fractions import Fraction

import engine  # noqa: F401  (lifts the int<->str digit cap)
from engine.money import div_round_half_even, to_usd_micro


class RoundHalfEvenTest(unittest.TestCase):
    def test_worked_example_1_rounds_each_entry(self):
        # Rate 1/2: +1, +3, -1, -3 -> 0, 2, 0, -2; the sum of rounded values is 0.
        got = [to_usd_micro(a, 1, 2) for a in (1, 3, -1, -3)]
        self.assertEqual(got, [0, 2, 0, -2])
        self.assertEqual(sum(got), 0)

    def test_ties_go_to_even_symmetrically(self):
        cases = {1: 0, 3: 2, 5: 2, 7: 4, -1: 0, -3: -2, -5: -2, -7: -4}
        for n, expected in cases.items():
            self.assertEqual(div_round_half_even(n, 2), expected, n)

    def test_non_ties_round_to_nearest(self):
        cases = {(2, 5): 0, (3, 5): 1, (-2, 5): 0, (-3, 5): -1,
                 (7, 3): 2, (8, 3): 3, (-7, 3): -2, (-8, 3): -3}
        for (n, d), expected in cases.items():
            self.assertEqual(div_round_half_even(n, d), expected, (n, d))

    def test_exact_division(self):
        self.assertEqual(div_round_half_even(-10, 5), -2)
        self.assertEqual(div_round_half_even(0, 7), 0)

    def test_worked_example_7_beyond_float_precision(self):
        amount = 9_007_199_254_740_993  # 2**53 + 1, not representable as a double
        self.assertEqual(to_usd_micro(amount, 1, 1), amount)
        self.assertEqual(to_usd_micro(-amount, 1, 1), -amount)

    def test_huge_values_survive_json_round_trip(self):
        amount = int("9" * 5000)  # past CPython's default 4300-digit str limit
        parsed = json.loads(json.dumps({"a": amount}))["a"]
        self.assertEqual(to_usd_micro(parsed, 3, 3), amount)

    def test_matches_fraction_reference(self):
        # round() on a Fraction is exact and rounds half to even.
        rng = random.Random(1234)
        for _ in range(20000):
            n = rng.randint(-10**30, 10**30)
            d = rng.randint(1, 10**6)
            self.assertEqual(div_round_half_even(n, d), round(Fraction(n, d)), (n, d))
        for _ in range(5000):  # small values hit ties often
            n = rng.randint(-50, 50)
            d = rng.choice([1, 2, 4, 6, 10])
            self.assertEqual(div_round_half_even(n, d), round(Fraction(n, d)), (n, d))

    def test_rejects_non_positive_denominator(self):
        with self.assertRaises(ValueError):
            div_round_half_even(1, 0)


if __name__ == "__main__":
    unittest.main()
