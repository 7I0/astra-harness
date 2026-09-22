#!/usr/bin/env python3
"""One portable entry point; optional tools load only when requested."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from . import dev_harness as h
except ImportError:
    import dev_harness as h

CORE = {"prepare", "verify", "check-result", "usage-manifest"}
EXTRA = {"inventory", "output-view", "context-map", "context-check", "checkpoint-init",
         "checkpoint-update", "checkpoint-inspect", "review-snapshot", "review-check"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    lead = argparse.ArgumentParser(add_help=False)
    lead.add_argument("--root", type=Path)
    known, remaining = lead.parse_known_args(argv)
    if remaining and remaining[0] in CORE:
        return h.main(argv)
    parser = argparse.ArgumentParser(description=__doc__, epilog="Core commands: " + ", ".join(sorted(CORE)))
    parser.add_argument("--root", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in sorted(EXTRA):
        sub = commands.add_parser(name)
        if name in {"context-check", "review-check"}:
            sub.add_argument("snapshot")
        if name in {"checkpoint-init", "checkpoint-update"}:
            sub.add_argument("path")
            sub.add_argument("packet")
        if name == "checkpoint-update":
            sub.add_argument("--expected-revision", type=int, required=True)
        if name == "checkpoint-inspect":
            sub.add_argument("paths", nargs="+")
            sub.add_argument("--packet")
        else:
            sub.add_argument("manifest")
    args = parser.parse_args(argv)
    try:
        root = h._workspace_root(args.root)

        def read(name):
            _, path = h._repo_path(name, must_exist=True, label="manifest", root=root)
            return h._load_json(path)

        if args.command in {"inventory", "output-view", "context-map", "context-check"}:
            try:
                from . import dev_harness_context as context
            except ImportError:
                import dev_harness_context as context
            manifest = read(args.manifest)
            if args.command == "inventory":
                result = context.guidance_inventory(root, manifest)
            elif args.command == "output-view":
                result = context.output_view(root, manifest)
            elif args.command == "context-map":
                result = context.context_map(root, manifest)
            else:
                result = context.context_check(root, read(args.snapshot), manifest)
        else:
            try:
                from . import dev_harness_state as state
            except ImportError:
                import dev_harness_state as state
            if args.command == "checkpoint-inspect":
                result = state.checkpoint_inspect(root, args.paths, read(args.packet) if args.packet else None)
            elif args.command == "checkpoint-init":
                result = state.checkpoint_init(root, args.path, read(args.packet), read(args.manifest))
            elif args.command == "checkpoint-update":
                result = state.checkpoint_update(root, args.path, read(args.packet), read(args.manifest),
                                                 expected_revision=args.expected_revision)
            elif args.command == "review-snapshot":
                result = state.review_snapshot(root, read(args.manifest))
            else:
                result = state.review_check(root, read(args.snapshot), read(args.manifest))
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (h.HarnessError, OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
