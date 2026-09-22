# Codex development harness

This is a portable reliability layer for bounded Codex development work. It
prepares task contracts, checks worker results, records explicit usage and
evidence, preserves interrupted work, and detects when a prior review is stale.
It runs locally with Python 3.9 or newer. The core has no runtime dependencies;
`pytest` is needed only for development checks.

The harness is intentionally a control and evidence layer. Native Codex executes
agents and the lead owns task selection and acceptance. This repository does not
intercept chats, choose the live model, discover conversation history, infer
billing, or silently approve semantic correctness. Those boundaries make its
records auditable instead of turning a requested route or a passing command into
an unsupported quality claim.

## Architecture

The complete workflow has three deliberately separate layers:

```text
native Codex + lead
  plan, dispatch, review, accept, and replan
              |
              v
development harness (this repository)
  packet contracts, routing declarations, evidence, usage,
  output excerpts, context maps, checkpoints, and review coverage
              |
              v
Atlas research runtime (companion project)
  economic gates, source-bound measurements, and Jev annotations
```

This separation is a feature. The development harness can be reused for other
projects without importing Atlas's database, market connectors, private runtime
configuration, or research data.

## Jev integration

Atlas uses Jev as a bounded annotation and review signal, not as an authority for
capital, access, queue, or kill decisions. The portable core is implemented in
[`integrations/jev/`](integrations/jev/), documented in
[`docs/jev-integration.md`](docs/jev-integration.md), with the machine-readable
contract in [`examples/jev-decision-contract.json`](examples/jev-decision-contract.json).

Jev's three decision surfaces are candidate triage and mechanism-family
annotation, evidence alignment against a precommitted falsifier, and adding a
review reason when supplied evidence is incomplete or ambiguous. Outbound state
is allowlisted and privacy-transformed. Results retain requested and returned
model identity, confidence, probabilities, errors, truncation, latency, and
provenance. A live failure may use a deterministic stub only when the record is
explicitly marked `stub`; it is never presented as a Jev verdict. The
deterministic Atlas gates remain authoritative, and Jev can only annotate or add
review. Features are disabled by default until frozen benchmark bars are met and
an operator intentionally enables them.

The client can call the TypeSafe System One endpoint when `TYPESAFE_API_KEY` is
explicitly supplied; otherwise it returns a clearly labelled deterministic stub.
No credential is read or transmitted by the test suite.

The portable layer deliberately has no database dependency: it returns a fully
labelled decision object that an Atlas adapter can persist in the Atlas evidence
ledger. Atlas's companion runtime adds that ledger, benchmark corpus, and
domain-specific deterministic gates. Keeping those pieces separate makes the
portable client runnable and reviewable without copying local research state.

## Quick start

Run the entry point directly from any working directory:

```sh
python /absolute/path/to/codex-development-harness/scripts/dev_harness_cli.py \
  --root /absolute/project prepare task.json --out runs/task-01

python /absolute/path/to/codex-development-harness/scripts/dev_harness_cli.py \
  --root /absolute/project verify runs/task-01/packet.json
```

Start from [`templates/task.json`](templates/task.json). Every path inside a
task or manifest is interpreted relative to `--root`. The inventory tool accepts
explicitly supplied absolute paths, and usage manifests may explicitly name
absolute telemetry paths. Write outputs to new run directories so earlier
evidence is preserved.

The complete command guide is in
[`docs/operating-guide.md`](docs/operating-guide.md). [`AGENTS.md`](AGENTS.md) is
a reusable global workflow template, while [`templates/AGENTS.md`](templates/AGENTS.md)
is a short project-level template. The files in [`agents/`](agents/) define three
optional Codex roles. Copy the desired `.toml` files to `~/.codex/agents/` for
user-wide discovery or to a project's `.codex/agents/` directory for project-only
discovery. [`config.example.toml`](config.example.toml) contains optional current
agent defaults; the standalone role files do not need `config_file` registration.

## What is proven

The v2 rollout was checked with 96 focused harness tests, plus 8 portable Jev
integration tests, and 21 installed-launcher invocations in an unrelated
temporary workspace. The checks cover packet and
result contracts, explicit usage accounting, conservative output retention,
scope-bound context maps, atomic checkpoint updates, stale-review detection, and
path boundaries. These are behavioral checks for the helper; they do not claim
matched live-model quality, Jev quality, complete telemetry, or subscription
savings. Missing cost and usage coverage remains unknown.

The companion Atlas Jev implementation has its own privacy, persistence,
adjudication, and offline-evaluation tests. Its results are evidence for the
research runtime, not a reason to weaken this harness's requirement for explicit
observations and independent acceptance.

## Development check

From this repository:

```sh
python -m pytest -q tests
```

No package installation is required when `pytest` is already available. The
source and test copies are bound to their original relative names and hashes in
[`PROVENANCE.json`](PROVENANCE.json). No license is asserted by this backup.
