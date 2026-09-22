# Codex development harness

This is a portable, private backup of a small development harness for preparing
bounded task packets, checking worker results, and retaining explicit evidence.
It runs locally with Python 3.9 or newer. The core has no runtime dependencies;
`pytest` is needed only for development checks.

The harness does not launch agents, select a live model, inspect conversation
history, discover telemetry, or approve work. It records requested routing and
validates declared inputs, outputs, checks, state, context, and review snapshots.
A person or lead agent still owns task selection and acceptance.

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

## Development check

From this repository:

```sh
python -m pytest -q tests
```

No package installation is required when `pytest` is already available. The
source and test copies are bound to their original relative names and hashes in
[`PROVENANCE.json`](PROVENANCE.json). No license is asserted by this backup.
