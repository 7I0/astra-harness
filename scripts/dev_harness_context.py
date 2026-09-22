"""Optional read-only context diagnostics; no model calls or command execution."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

try:
    from . import dev_harness as h
except ImportError:
    import dev_harness as h


def _fingerprint(path):
    data = path.read_bytes()
    return data, {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _fields(value, required, optional, label):
    if not isinstance(value, dict):
        raise h.HarnessError(label + " must be an object")
    h._exact_fields(value, required, optional, label)


def _paths(root, values, must_exist=True):
    if not isinstance(values, list) or not values:
        raise h.HarnessError("scope must be a nonempty list of workspace file paths")
    result = []
    for value in values:
        name, _ = h._repo_path(value, must_exist=must_exist, label="scope", root=root)
        if name in result:
            raise h.HarnessError("duplicate scope path: " + name)
        result.append(name)
    return sorted(result)


def guidance_inventory(root, manifest):
    """Inventory only explicitly supplied guidance; never return its prose/config body.

    Config parsing is deliberately limited to literal whitelisted settings, not an
    effective TOML loader. Runtime settings can only come from explicit observations.
    """
    _fields(manifest, {"files"}, {"catalog"}, "inventory")
    if not isinstance(manifest["files"], list):
        raise h.HarnessError("inventory.files must be a list")
    records, guidance, seen = [], {}, set()
    for item in manifest["files"]:
        _fields(item, {"path", "kind"}, set(), "inventory file")
        if item["kind"] not in {"instructions", "config", "skill"}:
            raise h.HarnessError("unsupported inventory kind")
        p = Path(h._nonempty_string(item["path"], "inventory path")).expanduser()
        p = (p if p.is_absolute() else root / p).resolve()
        if not p.is_file() or p in seen:
            raise h.HarnessError("missing or duplicate inventory file")
        seen.add(p)
        data, fp = _fingerprint(p)
        body = data.decode("utf-8")
        record = {"path": str(p), "kind": item["kind"], **fp,
                  "characters": len(body), "lines": len(body.splitlines())}
        if item["kind"] == "instructions":
            guidance[str(p)] = {re.sub(r"\s+", " ", line).strip() for line in body.splitlines()
                                if len(line.strip()) >= 24}
        elif item["kind"] == "skill":
            # Diagnostic size only, including multiline front matter conservatively.
            front = body.split("---", 2)[1] if body.startswith("---") and body.count("---") >= 2 else ""
            record["frontmatter_characters"] = len(front)
        else:
            settings, section = [], ""
            for line in body.splitlines():
                match = re.fullmatch(r"\s*\[([A-Za-z0-9_.-]+)\]\s*(?:#.*)?", line)
                if match:
                    section = match[1]
                match = re.fullmatch(r'\s*(model|model_reasoning_effort|service_tier|default_subagent_model|default_subagent_reasoning_effort|max_concurrent_threads_per_session)\s*=\s*("[A-Za-z0-9_.-]+"|[0-9]+)\s*(?:#.*)?', line)
                if not match:
                    continue
                key, raw = match.groups()
                value = json.loads(raw)
                allowed = (key in {"model", "default_subagent_model"} and value in h.SUPPORTED_ROUTE_EFFORTS
                           or key in {"model_reasoning_effort", "default_subagent_reasoning_effort"} and value in h.SUPPORTED_REASONING_EFFORTS
                           or key == "service_tier" and value in {"default", "standard", "priority", "flex", "auto"}
                           or key == "max_concurrent_threads_per_session" and type(value) is int)
                if allowed:
                    settings.append({"section": section, "key": key, "value": value})
            record["literal_configured_settings"] = settings
        records.append(record)
    overlaps = []
    for left in sorted(guidance):
        for right in sorted(guidance):
            if left < right:
                common = guidance[left] & guidance[right]
                overlaps.append({"left": left, "right": right, "shared_normalized_lines": len(common),
                                 "shared_characters": sum(map(len, common))})
    catalog = manifest.get("catalog")
    if catalog is not None:
        _fields(catalog, {"tools", "skill_descriptions"}, set(), "catalog")
        if not isinstance(catalog["tools"], list) or not isinstance(catalog["skill_descriptions"], list):
            raise h.HarnessError("catalog entries must be lists of strings")
        for item in catalog["tools"] + catalog["skill_descriptions"]:
            h._nonempty_string(item, "catalog entry")
        catalog = {"supplied_tools": len(catalog["tools"]),
                   "supplied_skill_descriptions": len(catalog["skill_descriptions"]),
                   "skill_description_characters": sum(map(len, catalog["skill_descriptions"])),
                   "coverage": "explicit supplied catalog only"}
    return {"version": 1, "files": sorted(records, key=lambda r: r["path"]),
            "overlap": overlaps, "catalog": catalog, "observed_runtime": None,
            "limit": "Character counts are not tokens. Literal config is not effective runtime state. Overlap detects exact normalized lines only."}


ALERT = re.compile(r"\b(fail(?:ed|ure|ures)?|error|warning|traceback|fatal|exception|counterexample|not.run|skipped|xfailed|xpassed)\b", re.I)


def output_view(root, manifest):
    _fields(manifest, {"path", "command", "exit_status", "format"}, {"max_lines", "max_line_characters"}, "output")
    name, _ = h._repo_path(manifest["path"], must_exist=True, label="raw output", root=root)
    h._nonempty_string(manifest["command"], "command")
    status = manifest["exit_status"]
    if status is not None and type(status) is not int:
        raise h.HarnessError("exit_status must be an integer or null for unobserved")
    fmt = manifest["format"]
    if fmt not in {"pytest", "rg", "git", "report", "raw"}:
        raise h.HarnessError("unsupported output format; use raw")
    limit = manifest.get("max_lines", 100)
    if type(limit) is not int or limit < 8 or limit > 2000:
        raise h.HarnessError("max_lines must be an integer from 8 to 2000")
    width = manifest.get("max_line_characters", 1000)
    if type(width) is not int or not 80 <= width <= 10000:
        raise h.HarnessError("max_line_characters must be from 80 to 10000")
    data, fp = _fingerprint(root / name)
    body = data.decode("utf-8", errors="replace")
    lines = body.splitlines()
    alerts = {i for i, line in enumerate(lines) if ALERT.search(line)}
    special = set(alerts)
    # Preserve pytest failure/setup sections when they fit; explicitly flag omitted
    # evidence otherwise. A summary saying 'passed' never overrides exit status.
    in_failure = False
    if fmt == "pytest":
        for i, line in enumerate(lines):
            if re.match(r"=+.*(FAILURES|ERRORS|warnings summary).*==", line):
                in_failure = True
            elif re.match(r"=+.*(short test summary info|[0-9]+ .* in ).*==", line):
                in_failure = False
            if in_failure:
                special.add(i)
    neighborhood = {j for i in special for j in range(max(0, i - 2), min(len(lines), i + 3))}
    if len(lines) <= limit:
        selected = set(range(len(lines)))
    else:
        anchors = set(range(min(3, len(lines)))) | set(range(max(0, len(lines) - 5), len(lines)))
        selected = set(sorted(anchors | alerts)[:limit])
        for pool in (sorted(neighborhood), list(range(len(lines)))):
            for i in pool:
                if len(selected) >= limit:
                    break
                selected.add(i)
    excerpt = [{"line": i + 1, "text": lines[i][:width],
                "omitted_characters": max(0, len(lines[i]) - width)} for i in sorted(selected)]
    return {"version": 1, "command": manifest["command"], "exit_status": status,
            "command_provenance": "caller supplied; helper did not execute command",
            "format": fmt, "raw": {"path": name, **fp}, "total_lines": len(lines),
            "omitted_lines": len(lines) - len(selected), "alert_lines": len(alerts),
            "omitted_alert_lines": len(alerts - selected),
            "omitted_failure_context_lines": len(special - selected),
            "truncated_excerpt_lines": sum(row["omitted_characters"] > 0 for row in excerpt),
            "decoded_with_replacement": body.encode("utf-8") != data,
            "excerpt": excerpt, "semantic_outcome": "not_assessed",
            "limit": "Open the retained raw file for omitted evidence; exit status alone does not prove acceptance."}


def context_map(root, manifest):
    _fields(manifest, {"scope"}, {"revision"}, "context map")
    scope = _paths(root, manifest["scope"])
    revision = manifest.get("revision")
    if revision is not None:
        h._nonempty_string(revision, "revision")
    files = []
    for name in scope:
        data, fp = _fingerprint(root / name)
        item = {"path": name, **fp, "symbols": [], "imports": [], "calls": [], "parse_error": None}
        if name.endswith(".py"):
            try:
                tree = ast.parse(data, filename=name)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        item["symbols"].append({"name": node.name, "kind": type(node).__name__, "line": node.lineno})
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        item["imports"].append({"module": getattr(node, "module", None),
                                               "level": getattr(node, "level", 0),
                                               "names": [alias.name for alias in node.names], "line": node.lineno})
                    elif isinstance(node, ast.Call):
                        fn = node.func
                        label = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                        if label:
                            item["calls"].append({"name": label, "line": node.lineno})
            except (SyntaxError, ValueError, UnicodeError) as exc:
                item["parse_error"] = type(exc).__name__
        else:
            item["parse_error"] = "non-Python source: fingerprint only"
        files.append(item)
    result = {"version": 1, "workspace_root": str(root.resolve()), "scope": scope,
              "revision_label": revision, "revision_provenance": "caller supplied; source hashes are authoritative",
              "files": files, "limit": "Scoped syntactic map, not resolved call graph or complete dependency proof. Expand scope for callers outside these files."}
    result["content_hash"] = h._sha256_bytes(h._canonical_bytes(result))
    return result


def context_check(root, snapshot, manifest):
    _fields(manifest, {"scope"}, set(), "context check")
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise h.HarnessError("invalid context snapshot")
    original = dict(snapshot)
    digest = original.pop("content_hash", None)
    if digest != h._sha256_bytes(h._canonical_bytes(original)):
        raise h.HarnessError("context snapshot hash mismatch")
    if snapshot.get("workspace_root") != str(root.resolve()):
        raise h.HarnessError("context snapshot belongs to another workspace")
    current = _paths(root, manifest["scope"], must_exist=False)
    changed, missing = [], []
    for record in snapshot["files"]:
        try:
            name, _ = h._repo_path(record["path"], must_exist=True, label="map file", root=root)
        except h.HarnessError:
            missing.append(record["path"])
            continue
        _, fp = _fingerprint(root / name)
        if fp != {k: record[k] for k in ("sha256", "bytes")}:
            changed.append(name)
    added = sorted(set(current) - set(snapshot["scope"]))
    removed = sorted(set(snapshot["scope"]) - set(current))
    return {"fresh": not (changed or missing or added or removed), "changed": changed,
            "missing": missing, "added_scope": added, "removed_scope": removed,
            "action": "Rebuild and inspect changed dependencies if stale; unchanged hashes do not prove scope completeness."}
