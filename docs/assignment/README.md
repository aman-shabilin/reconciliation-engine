# The Reconciliation Engine
## Data Engineer · AI-assisted take-home · Version 1.1

Build a local, recoverable settlement data product whose answers remain correct after late data, corrections, conflicting updates, retries and erasure requests. This is intentionally a demanding assessment of data engineering judgement and implementation.

**Claude, Codex and other coding assistants are allowed and expected.** You own the result. A fluent explanation without a working system will not pass.

### Time and equipment
- Timebox: **12 hours of active work**, submitted within seven calendar days. Stop at the timebox and document unfinished work. Agree any accessibility adjustment before starting.
- Use any language or local database. No required cloud account, paid service or company data. Evaluation runs on Linux with 4 CPU cores, 8 GiB RAM and 10 GiB free disk.
- Supply a Dockerfile and an executable `run` adapter. The runtime must work offline after the image is built. Pin dependencies and document the build command. Your own network access and AI use during development are allowed.
- AI access must be supplied or reimbursed equally by the hiring team. Record model/tool names, approximate spend and active hours; do not include credentials or private account history.

### Read in order
1. `SPECIFICATION.md`: binding semantics, schemas and command contract.
2. `WORKED_EXAMPLES.md`: calculations and expected edge behaviour.
3. `ASSESSMENT.md`: published scoring rubric and hard gates.
4. Inspect `data/`, `queries/` and `expected/`.
5. Run `python3 tools/check_output.py expected/q03.json` to see the public shape checker.

### Submit
Create a **single private GitHub repository** for your submission and invite the GitHub account **`whitecoat-data-team`** as a collaborator. Submit the repository URL. All code and all AI usage must live in that repository — we do not accept ZIP files or emailed attachments.

Keep the repository **unchanged once you have submitted the URL**, until evaluation is complete. If you find a defect you must fix, tell us rather than pushing silently.

The repository must contain your source, Dockerfile, `run`, lockfiles, tests and:
- A root `README.md`: how to build and run your system, and where your AI logs live in the repository.
- `DESIGN.md` (maximum 2,000 words): data model, invariants, state transitions, transaction boundary, time semantics, lineage, alternatives and limits.
- `OPERATIONS.md` (maximum 1,200 words): replay/rebuild, schema change, failed-run recovery, deletion, observability, performance measurements and a concrete incident runbook.
- `AI_LOG.md`: five consequential AI interactions, what you checked, at least two suggestions you rejected or corrected (if fewer occurred, say so honestly), tools/models, time and cost.
- **Raw coding-agent logs, committed to the repository.** Upload your coding agent session and chat logs — for example the `.jsonl` session files under `~/.claude/projects/`, or the equivalent export from Codex, Cursor or whichever tool you used — and state their location in your root `README.md`. Redact credentials and unrelated private account history; do not redact your own working process. If you used a tool that cannot export logs, say so in `AI_LOG.md` and describe what you used instead.
- `EVIDENCE.md`: exact commands and observed results for correctness, replay, crash recovery, privacy and scale; distinguish measured results from proposed design.
- A short `LIMITATIONS.md` listing anything incomplete. Do not conceal gaps with fabricated logs.

Do not commit generated bulk data, virtual environments, image layers, API keys or any other secret. Use environment variables or a gitignored `.env` file and document how to set them. Fixtures are fully synthetic. The hiring team will run your code on fresh datasets drawn from the same published rules and inspect the implementation. No production-quality UI is required.
