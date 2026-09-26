# Small cases you can calculate by hand

1. At a rate 1/2 microdollar per minor unit, amounts +1, +3, -1, -3 become 0, 2, 0, -2. Four entries in one group produce net 0 and count 4. Do not round their total instead.
2. Entry A v1 is +100 at rate 2/1: 200 microdollars. A later v2 is +70: the contribution becomes 140, not 340. A later v3 VOID removes it entirely. Before those knowledge cutoffs, earlier answers remain available.
3. Two different v2 payloads for A block A completely. Seeing v1 again does not restore it. One unambiguous v3 restores it. A repeat v3 in ledger schema 2 with the same parsed amount is a duplicate.
4. A merchant interval ends at 2026-01-02T00:00:00Z; an entry at that exact instant does not match it. If two selected intervals contain an instant, emit AMBIGUOUS_MERCHANT even if their region/tier values match.
5. A January 1 entry moves to January 2 in revision 2. A January 1 query at the newer cutoff loses the entry; a January 2 query gains it. Both dates must be invalidated in any cached aggregate.
6. A future FX correction must not alter an earlier knowledge snapshot. A committed future erasure does mask an earlier snapshot's customer token. Financial values remain the same.
7. Amount 9,007,199,254,740,993 at rate 1/1 must remain exactly 9,007,199,254,740,993. Parsing through floating point is a bug.

The public fixture includes these behaviours in combination. Public expected files assume **all public batches have been ingested**, so erasure masking is already applied even in q01. Compare business amounts at each cutoff and apply the global erasure rule separately.
