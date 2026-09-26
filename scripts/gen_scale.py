#!/usr/bin/env python3
"""Generate a seeded scale fixture following the published rules (spec §6-7).

    python3 scripts/gen_scale.py [--out scale] [--keys 200000] [--seed 42]

Writes batch_01.jsonl .. batch_12.jsonl and a few queries. Output is
gitignored bulk data: regenerate it, never commit it.

Shape: 200k ledger keys plus revisions, 12 batches, 100 merchants, 4
currencies. Mixed in at realistic rates: higher-version revisions (some
moving the effective date, merchant or currency), VOIDs, conflicting
highest versions later resolved, ledger v2 duplicates, exact redeliveries,
large amounts, missing merchants/FX, late FX and merchant corrections,
overlapping merchant intervals, unsupported schemas, synthetic PII
profiles, erasures and a post-erasure profile replay.

Skew: merchant M000 receives 40% of entries, 2026-01-15 receives 25% of
effective dates, and merchant M001 has 720 one-hour-ish validity intervals
(stresses interval lookup).
"""
import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

CURRENCIES = ["USD", "EUR", "JPY", "SGD"]
START = datetime(2026, 1, 1, tzinfo=timezone.utc)
DAYS = 59  # Jan 1 .. Feb 28
FAR_END = "2026-03-01T00:00:00Z"
NBATCH = 12


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Gen:
    def __init__(self, keys, seed):
        self.rng = random.Random(seed)
        self.keys = keys
        self.batches = {b: [] for b in range(1, NBATCH + 1)}
        self.n = 0

    def emit(self, batch, stream, payload, schema_version=1):
        self.n += 1
        self.batches[batch].append({"batch": batch, "row_id": f"s{self.n:09d}", "stream": stream,
                                    "schema_version": schema_version, "payload": payload})
        return self.batches[batch][-1]

    def redeliver(self, row):
        self.batches[row["batch"]].append(dict(row))

    def merchants(self):
        rng = self.rng
        for i in range(100):
            mid = f"M{i:03d}"
            if mid == "M001":  # skew: 720 contiguous intervals over 30 days
                t = START
                for k in range(720):
                    end = t + timedelta(hours=1)
                    self.emit(1, "merchants", {"merchant_id": mid, "valid_from": ts(t),
                                               "valid_to": ts(end) if k < 719 else FAR_END,
                                               "version": 1, "region": f"R{k % 5}",
                                               "tier": "hot"})
                    t = end
                continue
            self.emit(1, "merchants", {"merchant_id": mid, "valid_from": ts(START),
                                       "valid_to": FAR_END, "version": 1,
                                       "region": rng.choice(["EU", "US", "SG", "JP"]),
                                       "tier": rng.choice(["standard", "premium"])})
        # Overlap on M002 from Jan 20 -> AMBIGUOUS until batch 7 shortens v1.
        self.emit(1, "merchants", {"merchant_id": "M002", "valid_from": "2026-01-20T00:00:00Z",
                                   "valid_to": FAR_END, "version": 1, "region": "US",
                                   "tier": "premium"})
        self.emit(7, "merchants", {"merchant_id": "M002", "valid_from": ts(START),
                                   "valid_to": "2026-01-20T00:00:00Z", "version": 2,
                                   "region": "EU", "tier": "standard"})
        # Late tier change on M003, and an unreferenced conflicting merchant.
        self.emit(9, "merchants", {"merchant_id": "M003", "valid_from": ts(START),
                                   "valid_to": FAR_END, "version": 2, "region": "EU",
                                   "tier": "enterprise"})
        for region in ("A", "B"):
            self.emit(5, "merchants", {"merchant_id": "MX", "valid_from": ts(START),
                                       "valid_to": FAR_END, "version": 1, "region": region,
                                       "tier": "x"})

    def fx(self):
        rng = self.rng
        for d in range(DAYS):
            date = (START + timedelta(days=d)).strftime("%Y-%m-%d")
            for cur in CURRENCIES:
                if cur == "SGD" and d == 40:
                    continue  # missing FX day
                num, den = rng.randint(1, 2_000_000), rng.choice([1, 2, 3, 7, 100, 999_983])
                self.emit(1, "fx", {"currency": cur, "date": date, "version": 1,
                                    "usd_micro_num": num, "usd_micro_den": den})
                if rng.random() < 0.05:  # late correction
                    self.emit(rng.randint(2, NBATCH), "fx", {
                        "currency": cur, "date": date, "version": 2,
                        "usd_micro_num": num + 1, "usd_micro_den": den})
        for num in (1, 2):  # unreferenced FX conflict
            self.emit(3, "fx", {"currency": "XTS", "date": "2026-01-01", "version": 1,
                                "usd_micro_num": num, "usd_micro_den": 1})

    def ledger_payload(self, entry_id, version, customers):
        rng = self.rng
        r = rng.random()
        merchant = ("M000" if r < 0.40 else "M001" if r < 0.45 else
                    "M_UNKNOWN" if r < 0.451 else f"M{rng.randint(2, 99):03d}")
        if rng.random() < 0.25:
            day = 14
        else:
            day = rng.randrange(DAYS)
        eff = START + timedelta(days=day, seconds=rng.randrange(86400))
        amount = rng.randint(-10_000_000, 10_000_000)
        if rng.random() < 0.001:
            amount = rng.choice([-1, 1]) * rng.randint(10**15, 10**20)  # beyond 2**53
        currency = "ZZZ" if rng.random() < 0.0005 else rng.choice(CURRENCIES)
        return {"entry_id": entry_id, "version": version,
                "op": "VOID" if rng.random() < 0.02 else "POST", "merchant_id": merchant,
                "customer_token": rng.choice(customers), "effective_at": ts(eff),
                "currency": currency, "amount_minor": amount}

    def ledger(self, customers):
        rng = self.rng
        for k in range(self.keys):
            eid = f"E{k:07d}"
            b = rng.randint(2, NBATCH - 1)
            p = self.ledger_payload(eid, 1, customers)
            row = self.emit(b, "ledger", p)
            if rng.random() < 0.002:
                self.redeliver(row)  # exact redelivery
            if rng.random() < 0.10:  # higher version, later batch
                p2 = self.ledger_payload(eid, 2, customers)
                if rng.random() < 0.5:
                    p2["merchant_id"], p2["currency"] = p["merchant_id"], p["currency"]
                b2 = rng.randint(b, NBATCH)
                self.emit(b2, "ledger", p2)
                if rng.random() < 0.05:  # same v2 again in ledger schema 2
                    v2 = {k2: v for k2, v in p2.items() if k2 != "amount_minor"}
                    v2["amount_minor_text"] = str(p2["amount_minor"])
                    self.emit(rng.randint(b2, NBATCH), "ledger", v2, schema_version=2)
                if rng.random() < 0.03:  # conflicting v2, maybe resolved by v3
                    self.emit(b2, "ledger", {**p2, "amount_minor": p2["amount_minor"] + 1})
                    if rng.random() < 0.5:
                        self.emit(rng.randint(b2, NBATCH), "ledger",
                                  self.ledger_payload(eid, 3, customers))
            if rng.random() < 0.0002:
                self.emit(b, "ledger", {**p, "note": "x"}, schema_version=99)

    def people(self):
        customers = [f"C{i:05d}" for i in range(5000)]
        for c in customers:
            self.emit(1, "profiles", {"customer_token": c, "name": f"SYNTHETIC_NAME_{c}",
                                      "email": f"{c.lower()}@example.invalid"})
        erased = self.rng.sample(customers, 100)
        for c in erased:
            self.emit(NBATCH, "erasures", {"customer_token": c})
        self.emit(NBATCH, "profiles", {"customer_token": erased[0],
                                       "name": "SYNTHETIC_REPLAY", "email": "replay@example.invalid"})
        self.emit(6, "future_stream", {"note": "ignore"})
        return customers

    def write(self, out: Path):
        out.mkdir(parents=True, exist_ok=True)
        for b, rows in self.batches.items():
            self.rng.shuffle(rows)  # arrival order within a file is arbitrary
            with (out / f"batch_{b:02d}.jsonl").open("w", encoding="utf-8", newline="\n") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
        queries = {
            "full": (NBATCH, "2026-01-01", "2026-03-01"),
            "hot_day": (NBATCH, "2026-01-15", "2026-01-16"),
            "early_cutoff": (6, "2026-01-01", "2026-03-01"),
            "empty": (NBATCH, "2027-01-01", "2027-01-02"),
        }
        for name, (cutoff, s, e) in queries.items():
            (out / f"query_{name}.json").write_text(json.dumps(
                {"query_id": name, "cutoff_batch": cutoff, "start_date": s, "end_date": e}),
                encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "scale")
    ap.add_argument("--keys", type=int, default=200_000)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    g = Gen(args.keys, args.seed)
    g.merchants()
    g.fx()
    customers = g.people()
    g.ledger(customers)
    g.write(args.out)
    sizes = {b: len(r) for b, r in g.batches.items()}
    print(f"wrote {sum(sizes.values())} rows to {args.out}: {sizes}")


if __name__ == "__main__":
    main()
