# Efficient development across projects

Preserve the user's goal and selected model and effort. These are workflow
defaults; project instructions supply domain rules and acceptance.

- Astra owns direction, ambiguous reasoning, architecture, and consequential
  review. Sol High is an option for bounded implementation, extraction, and
  routine review, not a task-class requirement. Honor explicit model selection.
  Custom role model and effort pins override spawn values: use a matching role or
  a generic agent with explicit model and effort for other routes. Do not
  automatically downgrade effort or change providers. Record each delegated
  route and its reason; requested identity is not observed runtime identity.
- Work directly when handoff costs more. Normally use at most two independent
  workers plus the lead; reserve a third slot for focused review. Give workers
  fresh context, exact input paths, disjoint edits, acceptance criteria,
  invariants, and stop conditions. Avoid recursive delegation without a concrete
  need and avoid long inherited history for bounded edits.
- For substantive delegation, prepare and verify a v2 packet with
  `python /absolute/path/to/codex-development-harness/scripts/dev_harness_cli.py
  --root /absolute/project`. Replace the harness path for the local checkout;
  `docs/operating-guide.md` in that checkout documents the commands. The lead
  prepares, verifies, and accepts work. Read only the project's short handoff and
  relevant source and tests. Keep project facts out of global guidance.
- Accept observed checks and reviewed artifacts rather than a worker's success
  sentence or process exit. Hashes establish identity, not semantic quality. Use
  one focused independent review for consequential changes; skip review ceremony
  for trivial edits.
- Diagnose tool, input, and transport failures separately from reasoning. After
  two failed repairs, escalate with evidence. Never reset attempt, time, or call
  budgets. Reconcile partial files and external state after uncertain writes
  before retrying; preserve evidence and avoid racing writers.
- Retain raw output, return compact evidence, and reopen omitted material when
  needed. Run targeted checks once; repeat only after changes, failures, or new
  uncertainty. Do not duplicate research or repeatedly poll unchanged work.
- Use context maps, checkpoints, and review reuse only for unfamiliar or long
  tasks. Recheck source hashes, scope, dependencies, and assumptions before reuse.
  Keep simple edits free of bookkeeping. Never prune live history through a proxy.
- Measure accepted work across the lead, workers, review, retries, and repairs.
  Missing usage or cancelled-attempt cost remains unknown; subset tokens are not
  extra tokens. API price ratios do not prove subscription savings. Do not start
  a paid routing experiment, external model judge, or conversation export without
  task authorization.

Native Codex executes agents. This helper is not an automatic model router,
spending cap, permission sandbox, or guarantee of equivalent quality.
