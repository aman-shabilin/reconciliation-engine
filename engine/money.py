"""Exact integer money arithmetic. No floats anywhere."""


def div_round_half_even(numerator: int, denominator: int) -> int:
    """Return numerator/denominator rounded to the nearest integer, ties to even.

    Exact for arbitrarily large integers. Symmetric for negative values:
    -1/2 -> 0, -3/2 -> -2, just as 1/2 -> 0, 3/2 -> 2.
    """
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    # divmod floors, so 0 <= r < denominator and value = q + r/denominator.
    q, r = divmod(numerator, denominator)
    twice = 2 * r
    if twice > denominator or (twice == denominator and q & 1):
        q += 1
    return q


def to_usd_micro(amount_minor: int, num: int, den: int) -> int:
    """Convert one ledger entry to USD microdollars, rounded per entry."""
    return div_round_half_even(amount_minor * num, den)
