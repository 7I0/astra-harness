# Operating guide

The harness has one entry point:

```sh
python /absolute/path/to/codex-development-harness/scripts/dev_harness_cli.py \
  --root /absolute/project COMMAND [arguments]
```

Manifest and artifact paths are resolved beneath `--root` unless a command
explicitly documents otherwise. Inventory manifests may explicitly supply
absolute files, and usage manifests may explicitly supply absolute telemetry
paths. Commands emit JSON. Invalid input exits nonzero. Some diagnostics return
exit zero with a stale or incomplete result, so acceptance must inspect the
returned fields rather than relying on process status alone.

The relative templates and examples in this repository are source material. Copy
the file you want into the target project beneath `--root` before using a relative
command below. For example, copy `templates/task.json` to the target project's
`task.json`, or copy `examples/context.json` to its `examples/context.json`.

## Task packets and results

Create a task manifest from `templates/task.json`, then prepare and verify it:

```sh
python scripts/dev_harness_cli.py --root /absolute/project \
  prepare task.json --out runs/task-01
python scripts/dev_harness_cli.py --root /absolute/project \
  verify runs/task-01/packet.json
python scripts/dev_harness_cli.py --root /absolute/project \
  check-result runs/task-01/packet.json runs/task-01/result.json
```

`prepare` copies the manifest, fingerprints declared inputs, embeds the worker
prompt, and writes a result contract. Use a fresh output directory. `verify`
checks packet integrity. `check-result` validates the task identity, output and
log hashes, acceptance-to-check references, statuses, and unresolved findings.
It does not judge semantic quality; the lead must read the decisive changes and
evidence.

Routing fields record requested model, effort, reason, uncertainty, escalation
condition, and policy version. They do not change the model of a running task or
prove which model executed it. A custom role file may pin its own model and effort.

The current native route registry recognizes `gpt-6-astra`, `gpt-6-sol`,
`gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, and `gpt-5.5`.
GPT-6 Sol is the default for bounded implementation, routine review, and source
extraction; GPT-6 Astra remains the default for architecture, economic reasoning,
and consequential review. GPT-6 Luna and the older 5.x routes remain available
for explicit selection. These are native Codex route capabilities, not proof that
the requested route actually ran; observed identity still requires supplied
telemetry.

The effort ceiling is model-specific:

| Native model | Accepted efforts |
| --- | --- |
| `gpt-6-astra`, `gpt-6-sol`, `gpt-5.6-sol`, `gpt-5.6-terra` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| `gpt-6-luna`, `gpt-5.6-luna` | `low`, `medium`, `high`, `xhigh`, `max` |
| `gpt-5.5` | `low`, `medium`, `high`, `xhigh` |

This is a versioned native-Codex policy (`native-codex-2026-09-22`). It is kept
separate from API model documentation because a packet validates a local agent
route, not an arbitrary API request.

Version-1 packets retain their historical 5.6 Sol defaults so frozen packets can
still verify. Version-2 packets should use the current registry and explicit
selection fields when a route matters.

## Usage manifests

```sh
python scripts/dev_harness_cli.py --root /absolute/project \
  usage-manifest examples/usage.json
```

Declare attempts and every lead, worker, reviewer, retry, and repair explicitly.
Telemetry files are read only when listed. Missing or active records keep totals
unknown; known subtotals remain separate. Coverage is an attestation, not proof
that every agent was declared. Unknown cost stays `null`.

## Parallel work and runtime efficiency

The helper records and validates work around native Codex; it does not own the
live scheduler or provider retry loop. The dispatcher using a packet should keep
independent workers in flight, avoid polling one child while other independent
work is ready, and combine completions that arrive together before asking the
lead to re-plan. Preserve explicit barriers when a later stage depends on all
inputs, when labels must remain blind, or when the target resource is mutable and
must be serialized.

Capacity, overload, transport, invalid-input, and reasoning failures have
different remedies. A dispatcher may retry transient capacity or transport
failures with bounded exponential backoff, jitter, and any supplied retry hint.
Each retry is a distinct attempt with its own evidence. A fallback route must be
declared before use and is invalid for a packet whose model or effort is pinned.
Never treat a capacity failure as evidence that the worker reasoned poorly, and
never infer a cost saving from a model or API price ratio.

When runtime telemetry is available, retain one append-only event record per
logical stage or worker with: stable event and task identifiers, stage, queued,
started, and finished timestamps, requested and observed model/effort, outcome,
error class, retry hint, response identifiers, token counters, and cache counters.
This permits queue wait, execution time, retry rate, cache ratio, and accepted
work to be measured separately. Missing fields stay unknown; this guide does not
turn caller-supplied timestamps into observed runtime facts.

## Guidance inventory and retained output

```sh
python scripts/dev_harness_cli.py --root /absolute/project \
  inventory examples/inventory.json
python scripts/dev_harness_cli.py --root /absolute/project \
  output-view examples/output.json
```

Inventory reads only the listed instruction or configuration files and returns
hashes, sizes, exact-line overlap, and whitelisted literal configuration fields.
It does not expose file prose or secrets. The inventory example contains a
placeholder absolute home path; replace it before use.

Output view reads an already retained log and uses the caller-supplied command,
exit status, format, and line limit. It executes nothing. Failure and warning
markers receive priority, while omitted line and character counts remain visible.
Reopen the raw log when an excerpt is not decisive.

## Context maps

```sh
python scripts/dev_harness_cli.py --root /absolute/project \
  context-map examples/context.json > runs/context-snapshot.json
python scripts/dev_harness_cli.py --root /absolute/project \
  context-check runs/context-snapshot.json examples/context.json
```

Context maps cover only supplied files. Python definitions, imports, and syntactic
calls include line pointers; other formats receive hashes. This is not dependency
discovery. Include relevant callers, fixtures, and configuration, then rerun the
freshness check after changes.

## Recovery checkpoints

```sh
python scripts/dev_harness_cli.py --root /absolute/project \
  checkpoint-init runs/task-01/state.json runs/task-01/packet.json examples/state.json
python scripts/dev_harness_cli.py --root /absolute/project \
  checkpoint-update runs/task-01/state.json runs/task-01/packet.json examples/state.json \
  --expected-revision 0
python scripts/dev_harness_cli.py --root /absolute/project \
  checkpoint-inspect runs/task-01/state.json --packet runs/task-01/packet.json
```

Use checkpoints only for long or interrupted work. Updates use compare-and-swap
revisions and POSIX locks. Inspect files and worker state before retrying an
uncertain write. Clearing unresolved mutations or lost workers requires retained
observation evidence. Budgets never reset, and completed checkpoint state does not
mean the task result was accepted.

## Review snapshots

```sh
python scripts/dev_harness_cli.py --root /absolute/project \
  review-snapshot examples/review.json > runs/review-snapshot.json
python scripts/dev_harness_cli.py --root /absolute/project \
  review-check runs/review-snapshot.json examples/review-current.json
```

A snapshot binds declared file groups, dependencies, and named assumptions.
Changed fixtures or assumptions reopen dependent groups, and new files require
review. The lead remains responsible for complete dependency declarations and
truthful coverage.

## Suggested workflow

1. Keep simple work direct. For a bounded delegation, specify exact inputs,
   allowed edits, acceptance, invariants, stop conditions, and a maximum attempt
   count in a v2 task manifest.
2. Launch independent packets together when their scopes do not overlap. Give
   dependent stages an explicit barrier reason instead of waiting by habit.
3. Prepare and verify the packet. Give the worker the generated packet and its
   embedded prompt with fresh, minimal context.
4. Preserve raw checks. Require a result that follows the packet's result contract.
5. Run `check-result`, inspect the actual diff and decisive evidence, and request a
   focused independent review for consequential changes.
6. Retain the packet, result, and logs together. Treat hashes as identity evidence,
   not proof of correctness.
