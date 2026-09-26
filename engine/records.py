"""Parse transport rows into typed, normalised business records.

Each supported stream becomes a NamedTuple holding exactly its declared
business fields, so tuple equality *is* business equality: envelope fields
(row_id, batch, schema_version) and extra payload fields never take part.
Ledger v2 amounts are parsed to int here, so v1/v2 duplicates compare equal.

Profiles are discarded at parse time: name/email are never returned, so no
later layer can persist them. Unsupported rows keep only row_id and stream;
their payload is dropped because it may contain PII.
"""
import json
import re
from typing import NamedTuple, Optional, Union


class LedgerRev(NamedTuple):
    entry_id: str
    version: int
    op: str
    merchant_id: str
    customer_token: str
    effective_at: str
    currency: str
    amount_minor: int


class MerchantRev(NamedTuple):
    merchant_id: str
    valid_from: str
    version: int
    valid_to: str
    region: str
    tier: str


class FxRev(NamedTuple):
    currency: str
    date: str
    version: int
    usd_micro_num: int
    usd_micro_den: int


class Erasure(NamedTuple):
    customer_token: str


class Unsupported(NamedTuple):
    row_id: str
    stream: str


Record = Union[LedgerRev, MerchantRev, FxRev, Erasure, Unsupported]


class Row(NamedTuple):
    batch: int
    row_id: str
    record: Optional[Record]  # None for a discarded profile


SUPPORTED_SCHEMAS = frozenset({
    ("ledger", 1), ("ledger", 2), ("merchants", 1), ("fx", 1),
    ("profiles", 1), ("erasures", 1),
})

_INT_TEXT = re.compile(r"[+-]?[0-9]+")


def parse_int_text(text: str) -> int:
    """Parse a signed base-10 integer string exactly.

    Stricter than int(): rejects whitespace and '_' digit separators.
    """
    if not isinstance(text, str) or not _INT_TEXT.fullmatch(text):
        raise ValueError("amount_minor_text is not a signed base-10 integer")
    return int(text)


def _is_supported(stream, schema_version) -> bool:
    # type() check, not isinstance(): JSON `true` must not pass as version 1.
    return (type(schema_version) is int and isinstance(stream, str)
            and (stream, schema_version) in SUPPORTED_SCHEMAS)


def parse_row(obj: dict) -> Row:
    batch = obj["batch"]
    row_id = obj["row_id"]
    stream = obj["stream"]
    schema_version = obj["schema_version"]
    if not _is_supported(stream, schema_version):
        return Row(batch, row_id, Unsupported(row_id, stream))

    p = obj["payload"]
    if stream == "ledger":
        if schema_version == 1:
            amount = p["amount_minor"]
        else:
            amount = parse_int_text(p["amount_minor_text"])
        record = LedgerRev(p["entry_id"], p["version"], p["op"],
                           p["merchant_id"], p["customer_token"],
                           p["effective_at"], p["currency"], amount)
    elif stream == "merchants":
        record = MerchantRev(p["merchant_id"], p["valid_from"], p["version"],
                             p["valid_to"], p["region"], p["tier"])
    elif stream == "fx":
        record = FxRev(p["currency"], p["date"], p["version"],
                       p["usd_micro_num"], p["usd_micro_den"])
    elif stream == "erasures":
        record = Erasure(p["customer_token"])
    else:  # profiles: deliberately discarded, see module docstring
        record = None
    return Row(batch, row_id, record)


def parse_line(line: str) -> Row:
    return parse_row(json.loads(line))
