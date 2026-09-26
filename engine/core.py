"""Pure report computation: revision selection, conflicts, joins, output.

No I/O and no storage. The storage layer is responsible for handing in only
rows visible at the query's knowledge cutoff (committed, batch <= B) and the
set of all committed erasures. `evaluate` is an in-memory reference that
applies those rules to parsed rows directly; it is used by tests and as an
oracle for the storage-backed path.
"""
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, NamedTuple, Tuple

from .money import to_usd_micro
from .records import Erasure, FxRev, LedgerRev, MerchantRev, Row, Unsupported

REVISION_CONFLICT = "REVISION_CONFLICT"
UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
MISSING_MERCHANT = "MISSING_MERCHANT"
AMBIGUOUS_MERCHANT = "AMBIGUOUS_MERCHANT"
MISSING_FX = "MISSING_FX"


class Query(NamedTuple):
    query_id: str
    cutoff_batch: int
    start_date: str  # inclusive
    end_date: str    # exclusive


def parse_query(obj: dict) -> Query:
    return Query(obj["query_id"], obj["cutoff_batch"],
                 obj["start_date"], obj["end_date"])


def ledger_key(r: LedgerRev) -> str:
    return r.entry_id


def merchant_key(r: MerchantRev) -> str:
    return f"{r.merchant_id}|{r.valid_from}"


def fx_key(r: FxRev) -> str:
    return f"{r.currency}|{r.date}"


def select_latest(revisions: Iterable, key: Callable) -> Tuple[Dict[str, object], List[Tuple[str, int]]]:
    """Choose the highest business version per key.

    Identical payloads at the top version collapse (tuple equality is
    business equality). Two or more distinct payloads at the top version
    block the key: it is absent from the result and reported as a conflict
    at that version. There is no fallback to a lower version and no
    tie-break by arrival order or row_id.

    Returns (selected: key -> record, conflicts: [(key, version)]).
    """
    top: Dict[str, Tuple[int, set]] = {}
    for r in revisions:
        k = key(r)
        cur = top.get(k)
        if cur is None or r.version > cur[0]:
            top[k] = (r.version, {r})
        elif r.version == cur[0]:
            cur[1].add(r)
    selected = {}
    conflicts = []
    for k, (version, variants) in top.items():
        if len(variants) == 1:
            selected[k] = next(iter(variants))
        else:
            conflicts.append((k, version))
    return selected, conflicts


def _exception_sort_key(e: Tuple[str, str, str, object]):
    stream, key, code, version = e
    # null sorts before integers
    return (stream, key, code, (0, 0) if version is None else (1, version))


def build_report(query: Query,
                 ledger: Iterable[LedgerRev],
                 merchants: Iterable[MerchantRev],
                 fx: Iterable[FxRev],
                 unsupported: Iterable[Unsupported],
                 erased_tokens: Iterable[str]) -> dict:
    """Compute the exact output object for one query.

    `ledger`, `merchants`, `fx` and `unsupported` must already be restricted
    to committed rows with batch <= query.cutoff_batch. `erased_tokens` is
    every committed erasure, independent of the cutoff.
    """
    exceptions = set()

    sel_merchants, conflicts = select_latest(merchants, merchant_key)
    exceptions.update(("merchants", k, REVISION_CONFLICT, v) for k, v in conflicts)
    sel_fx, conflicts = select_latest(fx, fx_key)
    exceptions.update(("fx", k, REVISION_CONFLICT, v) for k, v in conflicts)
    sel_ledger, conflicts = select_latest(ledger, ledger_key)
    # All selected ledger conflicts are reported, even outside the date range.
    exceptions.update(("ledger", k, REVISION_CONFLICT, v) for k, v in conflicts)

    for u in unsupported:
        exceptions.add((u.stream, u.row_id, UNSUPPORTED_SCHEMA, None))

    intervals: Dict[str, List[MerchantRev]] = defaultdict(list)
    for m in sel_merchants.values():
        intervals[m.merchant_id].append(m)
    rates = {(f.currency, f.date): (f.usd_micro_num, f.usd_micro_den)
             for f in sel_fx.values()}
    erased = set(erased_tokens)

    groups: Dict[Tuple[str, str, str, str], List[int]] = {}
    trace = []
    for e in sel_ledger.values():
        if e.op != "POST":  # VOID: no join, contribution or trace
            continue
        date = e.effective_at[:10]
        if not (query.start_date <= date < query.end_date):
            continue
        # Half-open [valid_from, valid_to); timestamps compare as strings.
        matches = [m for m in intervals.get(e.merchant_id, ())
                   if m.valid_from <= e.effective_at < m.valid_to]
        if not matches:
            exceptions.add(("ledger", e.entry_id, MISSING_MERCHANT, e.version))
            continue
        if len(matches) > 1:
            exceptions.add(("ledger", e.entry_id, AMBIGUOUS_MERCHANT, e.version))
            continue
        rate = rates.get((e.currency, date))
        if rate is None:
            exceptions.add(("ledger", e.entry_id, MISSING_FX, e.version))
            continue

        m = matches[0]
        usd = to_usd_micro(e.amount_minor, *rate)
        g = groups.setdefault((date, m.merchant_id, m.region, m.tier), [0, 0])
        g[0] += usd
        g[1] += 1
        trace.append({
            "entry_id": e.entry_id,
            "version": e.version,
            "date": date,
            "merchant_id": m.merchant_id,
            "region": m.region,
            "tier": m.tier,
            "usd_micro": usd,
            "customer_token": None if e.customer_token in erased else e.customer_token,
        })

    settlements = [
        {"date": d, "merchant_id": mid, "region": region, "tier": tier,
         "net_usd_micro": net, "entry_count": count}
        for (d, mid, region, tier), (net, count) in sorted(groups.items())
    ]
    trace.sort(key=lambda t: t["entry_id"])
    return {
        "query_id": query.query_id,
        "settlements": settlements,
        "trace": trace,
        "exceptions": [
            {"stream": s, "key": k, "code": c, "version": v}
            for s, k, c, v in sorted(exceptions, key=_exception_sort_key)
        ],
    }


def evaluate(rows: Iterable[Row], query: Query) -> dict:
    """In-memory reference: treat every given row as committed.

    Applies the knowledge cutoff to business rows and the global rule to
    erasures. Unsupported rows are deduplicated by row_id.
    """
    ledger, merchants, fx, erased = [], [], [], set()
    unsupported: Dict[str, Unsupported] = {}
    for row in rows:
        r = row.record
        if isinstance(r, Erasure):
            erased.add(r.customer_token)  # independent of cutoff
        elif row.batch > query.cutoff_batch:
            continue
        elif isinstance(r, LedgerRev):
            ledger.append(r)
        elif isinstance(r, MerchantRev):
            merchants.append(r)
        elif isinstance(r, FxRev):
            fx.append(r)
        elif isinstance(r, Unsupported):
            unsupported.setdefault(r.row_id, r)
    return build_report(query, ledger, merchants, fx, unsupported.values(), erased)
