# Published assessment

Your submission is scored against the rubric below. Scores are evidence-based; a polished diagram cannot compensate for incorrect money. The hiring team does not promise or impose a percentile failure quota.

| Area | Points | Evidence |
|---|---:|---|
| Exact money, revision conflicts and schema normalisation | 22 | Public and fresh hidden result comparisons; numerical boundaries |
| Knowledge/effective time, joins and corrections | 22 | Historical queries, date movement, interval boundaries, late FX |
| Recovery, replay and incremental state | 18 | Both failpoints, restart, retry, reordered batches, gap rejection |
| Privacy and provenance | 12 | Erasure masking across cutoffs/replay; storage inspection; trace |
| Performance and operations | 13 | Repeatable host measurements, recovery runbook, scale note |
| Tests and engineering quality | 13 | Mutation-sensitive tests, dependency pinning, inspectable transactions |

### Hard gates
- Every exact-output correctness case in the agreed hidden suite passes. The suite is generated from this specification; questionable cases are manually checked before rejection.
- Both failure/recovery cases, replay and gap rejection pass with persistent state and truthful failpoints.
- No identity resurrection after committed erasure; no recoverable erased names/emails in managed storage or logs.
- Build and offline execution work within the agreed host limits; no hardcoded fixture answers, hidden-result access, fabricated evidence or undisclosed manual intervention.
- Your repository is accessible to `whitecoat-data-team`, contains your coding-agent logs, and contains no committed secrets.

Reviewers distinguish correctness failures, unimplemented work and infrastructure faults. If our environment is wrong, the run is repeated. You can challenge an ambiguous requirement; any clarification affecting semantics is supplied equally to all candidates. We assess engineering evidence, not writing style, token spend or the brand of AI tool used.
