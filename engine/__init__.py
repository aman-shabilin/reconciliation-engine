"""Reconciliation engine: bitemporal settlement reporting over JSONL batches."""
import sys

# Amounts are unbounded integers. CPython >= 3.11 caps int<->str conversion at
# 4300 digits by default, which would make json.loads/json.dumps reject a
# legitimately huge amount. Lift the cap for this process.
if hasattr(sys, "set_int_max_str_digits"):
    sys.set_int_max_str_digits(0)
