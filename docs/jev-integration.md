# Atlas Jev integration

This document describes how the Atlas research runtime uses Jev alongside the
portable development harness. It is a contract and architecture record; it does
not include Atlas production databases, credentials, signer files, or local
research captures.

## Role

Jev supplies bounded judgments over facts that Atlas has already assembled. It
does not discover opportunities, establish economic truth, authorize access,
change a queue, kill a lead, place an order, use a private key, deploy capital,
or replace the deterministic evidence gates.

The current decision types are:

| Decision | Output | Enforcement |
| --- | --- | --- |
| `candidate_triage` | candidate kind and mechanism family | annotate or expand; never shrink the search or queue |
| `evidence_alignment` | whether supplied evidence tests the precommitted falsifier | block or request review; never kill a candidate |
| `review_escalation` | an additional review reason | add or label review; never clear an existing review |

The portable implementation is in [`integrations/jev/`](../integrations/jev/):
`client.py` provides the live/stub client and `decision.py` provides the
allowlisted decision layer. The companion Atlas checkout adds the database
ledger, benchmark corpus, and domain-specific deterministic gates in
`atlas/jev_harness.py` and `atlas/scout/jev_client.py`.

## Request boundary

Every decision has a versioned decision type, item identifier, prompt and policy
version, and an allowlisted state. Unknown fields are rejected. Credential-shaped
strings, private keys, bearer tokens, cookies, seed phrases, and unapproved wallet
addresses are blocked or role-redacted before transmission. The outbound manifest
records the selected fields, types, byte size, wallet policy, and a digest.

Fact values are framed as untrusted data. Instructions embedded in observations
cannot become authorization. The framing is a prompt boundary, not a claim that a
model call is a security boundary.

## Result boundary

The portable decision layer normalizes each answer into a choice, confidence,
probabilities, and review reasons. Errors, truncation, model mismatch, invalid
schemas, low confidence, and failure to beat the neutral choice remain visible.
The returned object includes request ID, requested and returned model identity,
backend, latency, retry count, HTTP status, and a state digest so a caller can
persist it atomically with its own evidence ledger.

When no credential is available, or a transient vendor failure occurs, the client
may produce a deterministic rules stub. The returned model is then explicitly
`stub-rules-v0` and the backend is `stub`; the record cannot be mistaken for a
live Jev result. A definitive authentication rejection stops the run.

## Evaluation and activation

Atlas evaluates the three decision surfaces against frozen, source-bound labels
and deterministic baselines. The acceptance bars cover macro-F1, per-class
recall, checklist recall, evidence-alignment precision, review recall, and an
over-review ceiling. The benchmark is labelled provisional and is not presented
as a general model-quality claim.

The feature switches start disabled. An evaluation can report metrics and a
proposed activation, but production annotation is enabled only through an
intentional operator action after the bars and the evidence are reviewed. Jev
cannot authorize any action in the non-delegable action set.

## Relationship to this repository

The portable Jev package returns a decision object but does not silently write to
a database. An Atlas adapter can persist that object with the surrounding source
and evidence references. The development harness records the packet, requested
routing, supplied telemetry, retained logs, checkpoints, and review coverage around work that may
touch Atlas. It does not infer that a Jev request happened merely because a packet
asked for one. Conversely, Atlas's Jev ledger does not replace the harness's
lead/worker/reviewer accounting. Keeping those ledgers separate preserves the
difference between requested work, observed work, model output, and accepted
work.

The portable contract is summarized in
[`examples/jev-decision-contract.json`](../examples/jev-decision-contract.json).
