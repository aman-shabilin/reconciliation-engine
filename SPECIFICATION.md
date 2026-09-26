# Binding specification

## 1. Business problem
A marketplace needs daily merchant settlements in USD. Ledger updates, merchant classifications and FX rates can arrive late and be corrected retroactively. Reports must distinguish **effective time** from **knowledge time**. A privacy erasure must also survive old-data replay.

Implement an incremental local engine, not a program that returns saved answers. Full recomputation is permitted for small queries; explain and measure its limits. A batch may update any historical date.

## 2. Transport and types
Input is UTF-8 JSONL. Each file is one batch. Each line is a JSON object with `batch` (positive integer), globally unique `row_id` (string), `stream`, `schema_version`, and `payload`. All lines in a batch have the same batch number. Batches are nonempty. Delivery may be reordered and whole files retried. An exact row may be redelivered. A reused row_id with different bytes is out of scope; so are malformed JSON, corrupted files and hostile filesystem paths.

Known `schema_version` is 1 for every stream, plus ledger version 2. Unknown schema versions or stream names are quarantined as `UNSUPPORTED_SCHEMA`; they never take part in business revision selection. All supported payloads satisfy the field types below; testing will not introduce unspecified malformed values. Extra envelope fields and extra payload fields must be ignored for business equality. No unknown mandatory-field inference is expected.

All timestamps are UTC strings `YYYY-MM-DDTHH:MM:SSZ`. Dates are `YYYY-MM-DD`; strings compare chronologically. No local time zones or leap seconds. Integers are exact; amounts may exceed IEEE-754 safe integer range. Currency strings are opaque uppercase codes, not a fixed allowlist. All strings used as identifiers are ASCII without `|`. Monetary results are signed integer **USD microdollars**, never floats. No implicit FX value exists for USD.

### Ledger v1
`entry_id`, `version` (positive integer), `op` (`POST` or `VOID`), `merchant_id`, `customer_token`, `effective_at`, `currency`, `amount_minor` (signed integer).
Each entry is an independent signed movement: sale positive, refund negative. There is no parent-sale validation. A revision is a full replacement, not a delta; it may change date, currency, merchant, customer or amount. VOID payloads still contain all fields but generate no settlement or trace.

### Ledger v2
Same fields except `amount_minor` is replaced by `amount_minor_text`, a signed base-10 integer string. Parse it exactly and normalise to the v1 business representation **before** comparing revisions. v1/v2 semantically equal messages are duplicates.

### Merchants v1
`merchant_id`, `valid_from`, `valid_to`, `version`, `region`, `tier`.
Validity is a half-open UTC interval `[valid_from, valid_to)`. Each business key is `(merchant_id, valid_from)`; revisions fully replace that interval, including its end. All intervals have from < to. Overlaps are permitted in source data and must be handled as described below.

### FX v1
`currency`, `date`, `version`, `usd_micro_num`, `usd_micro_den`.
Business key is `(currency, date)`. The positive integer fraction gives **USD microdollars per one source minor unit**; do not apply another cents conversion. The denominator is > 0. FX date is the UTC date of the ledger entry.

### Profiles v1
`customer_token`, `name`, `email`.
These are synthetic PII provided only to test retention discipline. They are not necessary for settlement arithmetic. You may discard name/email immediately. No profile enrichment output is required.

### Erasures v1
`customer_token`.
Once an erasure batch is committed, that customer's token must be masked in **every snapshot whose read transaction begins after that commit**, including snapshots with an earlier knowledge cutoff. A concurrently running snapshot uses the committed state visible when its read transaction began. This deliberately overrides historical reproduction of identity. Amounts and other non-PII lineage remain unchanged. A tombstone containing the opaque token is allowed; names/emails must not remain recoverable in engine-managed storage, logs or caches after erasure. The immutable input fixtures are outside your managed state and are exempt. Do not alter them. Do not create additional unredacted raw-input archives.

## 3. Selection, conflict and join rules
For a query with `cutoff_batch=B`, consider supported ledger, merchant and FX rows whose batch <= B. Ingest must have committed every batch from 1 through B or snapshot must fail with exit 65 and publish no new output. Ingesting later batches must not change business results at earlier B. Erasure masking uses **all committed erasures**, independent of B.

For each business key, choose the highest business `version`, not the last-arriving row. Collapse copies with identical business fields at that version. Compare only the declared fields after normalising v2 amounts; row_id, batch and schema version are not business fields. If two or more different business payloads share the highest version, block that key and emit one `REVISION_CONFLICT`. Do not select a winner by row_id or fall back to a lower version. A later, higher, unambiguous version resolves that conflict. Report only conflicts at the selected highest version.

Ledger key: entry_id. Merchant key text: `merchant_id|valid_from`. FX key text: `currency|date`. All conflict exceptions have `stream`, `key`, `code`, `version`. Include merchant and FX conflicts even if no ledger references them. Unsupported-schema exceptions have stream as supplied, key=row_id, code=`UNSUPPORTED_SCHEMA`, version=null; deduplicate by row_id. Include unsupported rows <= B regardless of effective date. Profiles/erasures have no revision conflict semantics.

Select merchant/FX revisions first. A blocked key is absent for joins. For each selected non-conflicting POST **inside the query date range**:
1. Find selected merchant intervals for this merchant containing effective_at. Zero -> ledger exception `MISSING_MERCHANT`; more than one -> `AMBIGUOUS_MERCHANT`.
2. Find selected FX rate for (currency, UTC date). None -> ledger exception `MISSING_FX`.
3. Emit only the **first** join error using the above order. Error key is entry_id, version is its selected version, stream=`ledger`. No contribution and no trace for an excluded entry.
4. Otherwise compute amount_minor * num / den and round **each entry independently to nearest integer, ties to even**, symmetrically for negative amounts. Then sum. No early decimal rounding or rounding after aggregation.

Report all selected revision conflicts, even ledger conflicts outside the requested date range. Join errors apply only to selected POSTs in range. VOID entries produce no joins, contribution or trace, but a conflicted highest version is still a conflict even if one variant is VOID.

## 4. Query and exact output
Query JSON: `query_id`, `cutoff_batch`, `start_date`, `end_date`; dates are inclusive start, exclusive end. The reference fixtures contain examples. Empty results are valid.

Write one JSON object with exactly four top-level fields:
- `query_id`: copied from request.
- `settlements`: objects with `date`, `merchant_id`, `region`, `tier`, `net_usd_micro`, `entry_count`. Group by the first four fields. Include zero-net groups if at least one entry contributes. Sort by `(date, merchant_id, region, tier)` ascending.
- `trace`: one object per contributing entry with `entry_id`, `version`, `date`, `merchant_id`, `region`, `tier`, `usd_micro`, `customer_token`. Mask erased token as JSON null; otherwise preserve it. Sort by entry_id.
- `exceptions`: objects with `stream`, `key`, `code`, `version` as above, sorted by `(stream, key, code, version)`; null sorts before integers. No duplicate exception objects.

Use JSON integers, not strings, for all numeric outputs. No additional diagnostic fields. JSON object key order and whitespace do not matter. Array order matters. Diagnostics belong on stderr, without PII. See `expected/` for public answers.

## 5. Adapter and durability contract
The Docker image must place the executable adapter at `/app/run`, readable/executable by an arbitrary numeric UID. It must support a read-only root filesystem, writable `/state` and `/tmp`, and an explicitly mounted output directory. Do not require root or write to `/app`. From the submitted project root, all operations go through executable `./run`:

```sh
./run ingest --input /absolute/batch.jsonl --state /absolute/state
./run ingest --input /absolute/batch.jsonl --state /absolute/state --failpoint after-stage
./run ingest --input /absolute/batch.jsonl --state /absolute/state --failpoint before-commit
./run snapshot --query /absolute/query.json --state /absolute/state --out /absolute/result.json
```

Normal success exits 0. A supplied failpoint must unconditionally exit **86** on a previously unseen batch at the named point. `after-stage` means after durable staging and before visible publication; `before-commit` means immediately before the atomic visibility boundary. They may coincide if your design has one boundary; document why. Both leave the previous committed state visible. Restarting and replaying that batch without a failpoint must commit exactly once. Do not implement a fake early exit before any durable staging. Reviewers inspect and may kill the process at other points.

Batch payload, revision history, erasure effects and processed-batch checkpoint form one atomic visibility unit. A snapshot sees only committed batches. A failed snapshot must preserve an existing --out file byte for byte. Concurrent writers are out of scope. Concurrent readers must see a committed snapshot; explain how publication isolates readers. If you cannot physically erase PII atomically, discard it on ingest instead, or explain and demonstrate a key-destruction design.

A snapshot may use only --state and the query; it must not reread input files or rely on network or the current working directory. All durable mutable state belongs under --state; temporary files outside state must not carry PII. Reusing a state directory across process restarts is required. No in-memory-only solution.

## 6. Scale and operational work
The hidden scale fixture has 200,000 ledger keys plus revisions, up to 12 batches, 100 merchants and four currencies. On the stated 4-core/8-GiB host, target < 180 seconds total ingest and < 30 seconds per snapshot, peak memory < 4 GiB and state < 5 GiB. These are published scoring targets, not an unmeasured claim about your environment. Report host, commands, wall time, memory and disk measurements. Hiring reviewers repeat on one common host before scoring.

Also include a 600-word maximum design note within DESIGN.md for **one billion events/day**, a 30-day late-arrival window, 99.9% query availability, 15-minute correction visibility, and daily settlement close. State assumptions, partitioning, skew, compaction, consistency, costs in resource units, deployment/backfill cutover and what must change from your local solution. Do not claim you implemented this scale.

## 7. Test families disclosed in advance
Expect reordered ingestion; exact retries; equal revisions in different schema versions; conflicting highest revisions; higher-version conflict resolution; changed effective dates; late merchant/FX corrections; overlapping intervals; negative half-even ties; large integers; missing joins; unsupported schemas; old snapshots after future business updates; deletion followed by old-data replay; both failpoints; empty ranges; and scale/skew. Hidden inputs only vary data within these rules. No surprise domain rules will be introduced.
