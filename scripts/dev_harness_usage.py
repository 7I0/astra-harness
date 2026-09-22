"""Summarize explicitly selected local Codex telemetry, without conversation text.

Per-response usage is deduplicated; cumulative snapshots are NEVER summed.
An explicit manifest can aggregate supplied attempts and agents. Coverage is an
attestation, not automatic discovery. This is not a provider invoice.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
          "output_tokens", "reasoning_output_tokens", "total_tokens")


def summarize(files: list[Path], thread_id: str) -> dict:
    return _summarize(files, thread_id)[0]


def _summarize(files: list[Path], thread_id: str) -> tuple:
    records, sources = {}, []
    duplicate_records = 0
    incomplete_tails = 0
    for file in files:
        digest = hashlib.sha256()
        byte_count = 0
        with file.open("rb") as stream:
            for line_number, line in enumerate(stream, 1):
                digest.update(line)
                byte_count += len(line)
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    if line.endswith(b"\n") or stream.read(1):
                        raise ValueError(f"malformed telemetry at {file}:{line_number}") from exc
                    incomplete_tails += 1
                    break  # Only an incomplete final live record may be omitted.
                if not isinstance(item, dict):
                    raise ValueError(f"non-object telemetry at {file}:{line_number}")
                if item.get("type") != "token_usage_record":
                    continue
                p = item.get("payload", {})
                if not isinstance(p, dict):
                    raise ValueError("usage record payload must be an object")
                if p.get("thread_id") != thread_id:
                    continue
                key, usage = p.get("response_id"), p.get("usage")
                if not isinstance(key, str) or not key.strip() or not isinstance(usage, dict):
                    raise ValueError("usage record lacks response identity or counters")
                clean = {}
                for field in FIELDS:
                    value = usage.get(field)
                    if value is not None and (type(value) is not int or value < 0):
                        raise ValueError(f"invalid token counter: {field}")
                    clean[field] = value
                for part, whole in (("cached_input_tokens", "input_tokens"),
                                    ("reasoning_output_tokens", "output_tokens")):
                    if clean[part] is not None and clean[whole] is not None and clean[part] > clean[whole]:
                        raise ValueError(f"invalid token subset: {part} exceeds {whole}")
                if all(clean[k] is not None for k in ("input_tokens", "output_tokens", "total_tokens")):
                    if clean["total_tokens"] != clean["input_tokens"] + clean["output_tokens"]:
                        raise ValueError("inconsistent total_tokens: expected input_tokens + output_tokens")
                if key in records:
                    if records[key]["usage"] != clean:
                        raise ValueError("conflicting usage for one response; cannot total safely")
                    duplicate_records += 1
                else:
                    records[key] = {"usage": clean, "timestamp": item.get("timestamp")}
        sources.append({"path": str(file.resolve()), "bytes_read": byte_count, "read_prefix_sha256": digest.hexdigest()})
    totals, coverage, subtotals = {}, {}, {}
    for field in FIELDS:
        values = [r["usage"][field] for r in records.values()]
        known = [v for v in values if v is not None]
        coverage[field] = len(known)
        totals[field] = sum(known) if values and len(known) == len(values) else None
        subtotals[field] = sum(known) if known else None
    sizes = sorted(r["usage"]["input_tokens"] for r in records.values() if r["usage"]["input_tokens"] is not None)
    stamps = sorted(r["timestamp"] for r in records.values() if r["timestamp"])
    return {"thread_id": thread_id, "unique_responses": len(records), "deduplicated_records": duplicate_records,
            "incomplete_final_records": incomplete_tails,
            "first_record_at": stamps[0] if stamps else None, "last_record_at": stamps[-1] if stamps else None,
            "totals": totals, "coverage_responses": coverage, "observed_subtotals": subtotals,
            "input_per_response": {"median": median(sizes) if sizes else None,
                                   "p95_nearest_rank": sizes[max(0, (95 * len(sizes) + 99) // 100 - 1)] if sizes else None},
            "cost_usd": None, "sources": sources,
            "limits": ["Only supplied rollout prefixes for this exact thread; child-agent usage excluded.",
                       "Input totals count repeated processing; cached input is a subset, not additional input.",
                       "Reasoning output is a subset of output; never add it a second time.",
                       "No API-rate estimate, invoice reconciliation or measured savings claim.",
                       "Live files can grow after this snapshot; no conversation content is copied."]}, records


def _object(value, required, optional, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) - required - optional or required - set(value):
        raise ValueError(f"{label} has missing or unsupported fields")


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _coverage(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label}.coverage_complete must be boolean")
    return value


def _status(value):
    if value not in {"completed", "failed", "interrupted", "cancelled", "active", "unknown"}:
        raise ValueError("unsupported accounting status")
    return value


def _aggregate(reports, complete):
    """Unknown components invalidate totals without discarding observed counters."""
    totals, subtotals = {}, {}
    for field in FIELDS:
        observed = [r["observed_subtotals"][field] for r in reports
                    if r["observed_subtotals"][field] is not None]
        subtotals[field] = sum(observed) if observed else None
        totals[field] = (sum(r["totals"][field] for r in reports)
                         if complete and reports and all(r["totals"][field] is not None for r in reports)
                         else None)
    return {"totals": totals, "observed_subtotals": subtotals,
            "coverage_complete": complete, "cost_usd": None}


def summarize_manifest(manifest_path: Path, root: Path) -> dict:
    """Sum only explicitly supplied telemetry; never discover history or infer billing.

    Each thread may occur once across the entire manifest. Reusing a thread for
    multiple attempts is rejected because whole-thread files cannot partition it.
    Relative telemetry paths resolve from root; absolute paths are explicit opt-in.
    """
    # Works both as a package module and in the portable installed directory.
    try:
        from .dev_harness import _load_json
    except ImportError:
        from dev_harness import _load_json
    root = Path(root).resolve(strict=True)
    manifest_path = Path(manifest_path)
    value = _load_json(manifest_path if manifest_path.is_absolute() else root / manifest_path)
    _object(value, {"version", "task_id", "coverage_complete", "attempts"}, set(), "usage manifest")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("usage manifest version must be 1")
    task_id = _text(value["task_id"], "task_id")
    complete = _coverage(value["coverage_complete"], "manifest")
    if not isinstance(value["attempts"], list) or not value["attempts"]:
        raise ValueError("attempts must be a non-empty list")
    attempt_ids, thread_ids, response_owners = set(), set(), {}
    attempts = []
    for attempt in value["attempts"]:
        _object(attempt, {"attempt_id", "status", "coverage_complete", "agents"}, set(), "attempt")
        attempt_id = _text(attempt["attempt_id"], "attempt_id")
        if attempt_id in attempt_ids:
            raise ValueError("duplicate attempt_id")
        attempt_ids.add(attempt_id)
        attempt_status = _status(attempt["status"])
        attempt_complete = _coverage(attempt["coverage_complete"], "attempt")
        attempt_complete = attempt_complete and attempt_status not in {"active", "unknown"}
        if not isinstance(attempt["agents"], list):
            raise ValueError("agents must be a list")
        agents = []
        for agent in attempt["agents"]:
            _object(agent, {"thread_id", "role", "status", "requested_model", "requested_effort",
                            "telemetry_files", "coverage_complete"}, {"observed_identity"}, "agent")
            thread_id = _text(agent["thread_id"], "thread_id")
            if thread_id in thread_ids:
                raise ValueError("thread_id overlaps agents or attempts; cannot safely partition usage")
            thread_ids.add(thread_id)
            role = _text(agent["role"], "role")
            status = _status(agent["status"])
            requested = {"model": _text(agent["requested_model"], "requested_model"),
                         "reasoning_effort": _text(agent["requested_effort"], "requested_effort")}
            observed = agent.get("observed_identity")
            if observed is not None:
                _object(observed, {"source"}, {"model", "reasoning_effort"}, "observed_identity")
                for key, entry in observed.items():
                    _text(entry, f"observed_identity.{key}")
            agent_complete = _coverage(agent["coverage_complete"], "agent")
            agent_complete = agent_complete and status not in {"active", "unknown"}
            if not isinstance(agent["telemetry_files"], list):
                raise ValueError("telemetry_files must be a list")
            files, missing = [], []
            for entry in agent["telemetry_files"]:
                path = Path(_text(entry, "telemetry file"))
                path = path if path.is_absolute() else root / path
                if not path.is_file():
                    missing.append(str(path))
                else:
                    files.append(path)
            report, records = _summarize(files, thread_id)
            for response_id in records:
                if response_id in response_owners:
                    raise ValueError("response identity overlaps threads; cannot count twice")
                response_owners[response_id] = thread_id
            reasons = []
            if not agent_complete:
                reasons.append("coverage not attested or status not terminal")
            if missing:
                reasons.append("supplied telemetry files missing")
            if not records:
                reasons.append("no matching per-response records")
            if report["incomplete_final_records"]:
                reasons.append("partial final telemetry record")
            agent_complete = not reasons
            report.update({"role": role, "status": status, "requested_identity": requested,
                           "observed_identity": observed, "missing_files": missing,
                           "coverage_complete": agent_complete, "incomplete_reasons": reasons})
            if not agent_complete:
                report["totals"] = {field: None for field in FIELDS}
            agents.append(report)
        attempt_complete = attempt_complete and bool(agents) and all(a["coverage_complete"] for a in agents)
        attempts.append({"attempt_id": attempt_id, "status": attempt_status, "agents": agents,
                         **_aggregate(agents, attempt_complete)})
    complete = complete and all(a["coverage_complete"] for a in attempts)
    return {"version": 1, "task_id": task_id, "attempts": attempts,
            **_aggregate(attempts, complete),
            "limits": ["Coverage relies on explicit manifest attestations; omitted agents cannot be discovered.",
                       "Requested identity is never treated as observed identity; supplied observations remain scoped metadata.",
                       "Missing counters remain unknown; observed subtotals are not whole-task totals.",
                       "Cached input and reasoning output are subsets, never added to their parent counters.",
                       "Cancelled and interrupted work is retained; billing remains unknown without account evidence.",
                       "Only supplied numeric usage records are read; no conversation text is copied."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    report = summarize(args.files, args.thread_id)
    text = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.out:
        with args.out.open("x") as output:
            output.write(text)
    print(text)


if __name__ == "__main__":
    main()
