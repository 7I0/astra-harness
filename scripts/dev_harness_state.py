"""Optional offline checkpoints and snapshot-bound review reuse.

POSIX advisory locks coordinate cooperating local writers. Hashes bind artifacts,
not the truth of a caller's worker observations or review conclusions. Completion
here is bookkeeping, never acceptance. No workers or commands are dispatched.
"""
from __future__ import annotations

import copy
import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    from . import dev_harness as h
except ImportError:  # portable installed scripts
    import dev_harness as h

STATES = {"active", "waiting", "reconciliation_required", "replan_required", "completed"}
MUTABLE = {"phase", "evidence", "active_workers", "attempts_used", "next_action", "state", "unresolved_mutations"}


def _fields(obj, required, optional=(), label="manifest"):
    if not isinstance(obj, dict):
        raise h.HarnessError(label + " must be an object")
    h._exact_fields(obj, set(required), set(optional), label)


def _integer(value, label):
    if type(value) is not int or value < 0:
        raise h.HarnessError(label + " must be a non-negative integer")


def _strings(value, label):
    return h._string_list(value, label, allow_empty=True)


def _seal(obj):
    value = copy.deepcopy(obj)
    value["content_hash"] = h._sha256_bytes(h._canonical_bytes(value))
    return value


def _unseal(obj):
    if not isinstance(obj, dict):
        raise h.HarnessError("snapshot must be an object")
    value = copy.deepcopy(obj)
    expected = value.pop("content_hash", None)
    if expected != h._sha256_bytes(h._canonical_bytes(value)):
        raise h.HarnessError("snapshot content hash mismatch")
    return value


def _records(root, records, *, current=True):
    if not isinstance(records, list):
        raise h.HarnessError("evidence must be a list")
    paths = []
    stale = []
    for record in records:
        _fields(record, {"path", "sha256", "bytes"}, label="fingerprint")
        name, path = h._repo_path(record["path"], must_exist=False, label="fingerprint", root=root)
        if not isinstance(record["sha256"], str) or not h.SHA256_RE.fullmatch(record["sha256"]):
            raise h.HarnessError("invalid fingerprint SHA-256")
        _integer(record["bytes"], "fingerprint bytes")
        if not path.is_file() or h._file_fingerprint(path) != (record["sha256"], record["bytes"]):
            stale.append(name)
        paths.append(name)
    if len(set(paths)) != len(paths):
        raise h.HarnessError("duplicate evidence paths")
    if current and stale:
        raise h.HarnessError("stale evidence: " + ", ".join(stale))
    return stale


def _mutable(root, manifest, max_attempts, *, current=True):
    _fields(manifest, MUTABLE, {"reconciled", "resolved_workers"})
    for field in ("phase", "next_action"):
        h._nonempty_string(manifest[field], field)
    if manifest["state"] not in STATES:
        raise h.HarnessError("invalid checkpoint state")
    _integer(manifest["attempts_used"], "attempts_used")
    if manifest["attempts_used"] > max_attempts:
        raise h.HarnessError("attempt budget exceeded")
    _strings(manifest["active_workers"], "active_workers")
    _strings(manifest["unresolved_mutations"], "unresolved_mutations")
    _records(root, manifest["evidence"], current=current)
    if manifest["state"] == "reconciliation_required" and not manifest["unresolved_mutations"]:
        raise h.HarnessError("reconciliation requires an unresolved mutation identity")
    if manifest["unresolved_mutations"] and manifest["state"] != "reconciliation_required":
        raise h.HarnessError("unknown mutation outcome requires reconciliation")
    if manifest["state"] == "completed" and manifest["active_workers"]:
        raise h.HarnessError("active or lost workers prevent completion")


def _read_state(root, path):
    state = _unseal(h._load_json(path))
    _fields(state, MUTABLE | {"version", "kind", "workspace_root", "task_id", "packet_content_hash", "max_attempts", "revision", "requires_new_packet"})
    if state["version"] != 1 or state["kind"] != "development_checkpoint":
        raise h.HarnessError("unsupported checkpoint")
    if state["workspace_root"] != str(root):
        raise h.HarnessError("checkpoint belongs to another workspace")
    h._nonempty_string(state["task_id"], "task_id")
    if not isinstance(state["packet_content_hash"], str) or not h.SHA256_RE.fullmatch(state["packet_content_hash"]):
        raise h.HarnessError("invalid packet binding")
    _integer(state["revision"], "revision")
    _integer(state["max_attempts"], "max_attempts")
    if type(state["requires_new_packet"]) is not bool or (state["state"] == "replan_required" and not state["requires_new_packet"]):
        raise h.HarnessError("invalid persistent replan requirement")
    _mutable(root, {key: state[key] for key in MUTABLE}, state["max_attempts"], current=False)
    return state


@contextmanager
def _locked(root, name):
    _, path = h._repo_path(name, must_exist=False, label="checkpoint", root=root)
    # The persistent lock inode is never unlinked; renaming the data cannot split it.
    lock_name = str(path.relative_to(root)) + ".lock"
    _, lock = h._repo_path(lock_name, must_exist=False, label="checkpoint lock", root=root)
    if (root / lock_name).is_symlink():
        raise h.HarnessError("checkpoint lock must not be a symlink")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            # Resolve again after acquiring lock to catch ordinary path changes.
            _, checked = h._repo_path(name, must_exist=False, label="checkpoint", root=root)
            if checked != path:
                raise h.HarnessError("checkpoint path changed while acquiring lock")
            yield path
    except OSError as exc:
        raise h.HarnessError("checkpoint IO failed: " + str(exc)) from exc


def _write(path, value):
    sealed = _seal(value)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(h._canonical_bytes(sealed) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return sealed


def _drift(root, state, packet, proposed_evidence=None):
    problems = []
    if packet.get("task_id") != state["task_id"] or packet.get("workspace_root", str(root)) != str(root):
        raise h.HarnessError("packet belongs to another task or workspace")
    if packet.get("packet_content_hash") != state["packet_content_hash"]:
        problems.append("packet contract changed; old binding retained")
    # Explicit current evidence may supersede prepared *editable* inputs only.
    # Immutable source and other prior evidence still require quarantine on drift.
    editable = {}
    for record in state["evidence"] + (proposed_evidence or []):
        if record["path"] in packet.get("allowed_edits", []):
            editable[record["path"]] = (record["sha256"], record["bytes"])
    try:
        h.verify_packet(packet, editable_outputs=editable, root=root)
    except h.HarnessError as exc:
        problems.append(str(exc))
    prior = [record for record in state["evidence"] if record["path"] not in editable]
    stale = _records(root, prior, current=False)
    # Non-input editable evidence must still match actual files.
    for name, (digest, size) in editable.items():
        stale.extend(_records(root, [{"path": name, "sha256": digest, "bytes": size}], current=False))
    if stale:
        problems.append("prior evidence changed: " + ", ".join(stale))
    return problems


def checkpoint_init(root: Path, path: str, packet: dict, manifest: dict) -> dict:
    """Create revision 0; the parent directory must exist. Never overwrite."""
    root = h._workspace_root(Path(root))
    with _locked(root, path) as target:
        if target.exists():
            raise h.HarnessError("checkpoint already exists")
        h.verify_packet(packet, root=root)
        _fields(manifest, MUTABLE)
        _mutable(root, manifest, packet["max_attempts"])
        state = dict(copy.deepcopy(manifest), version=1, kind="development_checkpoint",
                     workspace_root=str(root), task_id=packet["task_id"],
                     packet_content_hash=packet["packet_content_hash"],
                     max_attempts=packet["max_attempts"], revision=0,
                     requires_new_packet=manifest["state"] == "replan_required")
        return _write(target, state)


def _observations(root, records, key, required):
    if not isinstance(records, list):
        raise h.HarnessError("observations must be a list")
    seen = set()
    for record in records:
        _fields(record, {key, "evidence"}, label="observation")
        identity = h._nonempty_string(record[key], key)
        if identity in seen or identity not in required:
            raise h.HarnessError("unexpected or duplicate observation: " + identity)
        if not record["evidence"]:
            raise h.HarnessError("observed evidence required for " + identity)
        _records(root, record["evidence"])
        seen.add(identity)
    if seen != required:
        raise h.HarnessError("observed evidence required for: " + ", ".join(sorted(required - seen)))


def checkpoint_update(root: Path, path: str, packet: dict, manifest: dict, *, expected_revision: int) -> dict:
    """CAS a full mutable manifest. Quarantine preserves the original binding."""
    root = h._workspace_root(Path(root))
    _integer(expected_revision, "expected_revision")
    with _locked(root, path) as target:
        old = _read_state(root, target)
        if old["revision"] != expected_revision:
            raise h.HarnessError("stale checkpoint revision")
        _mutable(root, manifest, old["max_attempts"], current=False)
        quarantine = manifest["state"] in {"replan_required", "reconciliation_required"}
        drift = _drift(root, old, packet, manifest["evidence"])
        if drift and not quarantine:
            raise h.HarnessError("quarantine required: " + "; ".join(drift))
        # Quarantine can retain stale old evidence, never introduce stale evidence.
        retained = [record for record in manifest["evidence"] if record not in old["evidence"]]
        _records(root, retained if quarantine else manifest["evidence"])
        if manifest["attempts_used"] < old["attempts_used"]:
            raise h.HarnessError("attempts_used cannot decrease")
        if old["state"] == "completed" and manifest["state"] != "completed":
            raise h.HarnessError("completed checkpoint is terminal; create a new task")
        if old["requires_new_packet"] and not quarantine:
            raise h.HarnessError("replan requires a new packet and checkpoint")
        removed = set(old["unresolved_mutations"]) - set(manifest["unresolved_mutations"])
        _observations(root, manifest.get("reconciled", []), "mutation", removed)
        departed = set(old["active_workers"]) - set(manifest["active_workers"])
        _observations(root, manifest.get("resolved_workers", []), "worker", departed)
        # Keep reconciliation evidence in the durable checkpoint, not only in the call.
        evidence = copy.deepcopy(manifest["evidence"])
        supplied_paths = {item["path"] for item in evidence}
        for item in old["evidence"]:
            if item["path"] in packet.get("allowed_edits", []) and item["path"] not in supplied_paths:
                # _drift used this binding to validate the authorized working file.
                # Dropping it would make the just-written checkpoint stale on inspect.
                evidence.append(copy.deepcopy(item))
                supplied_paths.add(item["path"])
        for records in (manifest.get("reconciled", []), manifest.get("resolved_workers", [])):
            for record in records:
                for item in record["evidence"]:
                    if item not in evidence:
                        evidence.append(copy.deepcopy(item))
        _records(root, evidence, current=not quarantine)
        new = dict(old, **{key: copy.deepcopy(manifest[key]) for key in MUTABLE})
        new["evidence"] = evidence
        new["requires_new_packet"] = old["requires_new_packet"] or manifest["state"] == "replan_required"
        new["revision"] += 1
        return _write(target, new)


def checkpoint_inspect(root: Path, paths: list[str], packet=None) -> dict:
    """Inspect all explicit candidates; never silently choose an active run."""
    root = h._workspace_root(Path(root))
    _strings(paths, "candidate paths")
    candidates = []
    seen = set()
    for name in paths:
        _, path = h._repo_path(name, must_exist=True, label="candidate", root=root)
        if path in seen:
            raise h.HarnessError("duplicate canonical candidate")
        seen.add(path)
        state = _read_state(root, path)
        drift = _drift(root, state, packet) if packet is not None else [
            "prior evidence changed: " + name for name in _records(root, state["evidence"], current=False)
        ]
        candidates.append({"path": name, "checkpoint": _seal(state), "drift": drift,
                           "reconciliation_required": bool(state["active_workers"] or state["unresolved_mutations"] or state["state"] == "reconciliation_required")})
    return {"status": "ambiguous" if len(candidates) > 1 else ("found" if candidates else "missing"),
            "candidates": candidates, "acceptance": "not_assessed"}


def _paths(root, paths, label, *, current=False):
    _strings(paths, label)
    for name in paths:
        h._repo_path(name, must_exist=current, label=label, root=root)
    return paths


def _assumptions(values):
    if not isinstance(values, dict):
        raise h.HarnessError("assumptions must be an object")
    for key, value in values.items():
        h._nonempty_string(key, "assumption name")
        h._nonempty_string(value, "assumption value")


def _review_manifest(root, manifest, *, current):
    _fields(manifest, {"scope", "assumptions", "groups"})
    scope = _paths(root, manifest["scope"], "scope", current=current)
    _assumptions(manifest["assumptions"])
    if not isinstance(manifest["groups"], list):
        raise h.HarnessError("groups must be a list")
    names = set()
    files = set(scope)
    for group in manifest["groups"]:
        _fields(group, {"id", "files", "dependencies", "assumptions"}, label="group")
        name = h._nonempty_string(group["id"], "group id")
        if name in names:
            raise h.HarnessError("duplicate group id")
        names.add(name)
        if not group["files"]:
            raise h.HarnessError("review group must cover files")
        _paths(root, group["files"], "group files", current=current)
        _paths(root, group["dependencies"], "group dependencies", current=current)
        if not set(group["files"]) <= set(scope):
            raise h.HarnessError("reviewed file outside declared scope")
        _strings(group["assumptions"], "group assumptions")
        if not set(group["assumptions"]) <= set(manifest["assumptions"]):
            raise h.HarnessError("unknown group assumption")
        files.update(group["dependencies"])
    return files


def review_snapshot(root: Path, manifest: dict) -> dict:
    """Bind caller-declared review groups to exact files and assumption values."""
    root = h._workspace_root(Path(root))
    files = _review_manifest(root, manifest, current=True)
    records = []
    for name in sorted(files):
        _, path = h._repo_path(name, must_exist=True, label="review file", root=root)
        digest, size = h._file_fingerprint(path)
        records.append({"path": name, "sha256": digest, "bytes": size})
    return _seal(dict(copy.deepcopy(manifest), version=1, kind="development_review", workspace_root=str(root), evidence=records))


def review_check(root: Path, snapshot: dict, manifest: dict) -> dict:
    """Return retained/reopened groups and uncovered *current intended* scope."""
    root = h._workspace_root(Path(root))
    old = _unseal(snapshot)
    _fields(old, {"scope", "assumptions", "groups", "version", "kind", "workspace_root", "evidence"})
    if old["version"] != 1 or old["kind"] != "development_review" or old["workspace_root"] != str(root):
        raise h.HarnessError("review snapshot belongs to another workspace or version")
    _fields(manifest, {"scope", "assumptions"})
    scope = _paths(root, manifest["scope"], "scope")
    _assumptions(manifest["assumptions"])
    files = _review_manifest(root, {key: old[key] for key in ("scope", "assumptions", "groups")}, current=False)
    changed = set(_records(root, old["evidence"], current=False))
    if files != {record["path"] for record in old["evidence"]}:
        raise h.HarnessError("review fingerprint coverage does not match declared scope")
    changed_assumptions = {key for key, value in old["assumptions"].items() if manifest["assumptions"].get(key) != value}
    groups, covered, reopened_files = [], set(), set()
    for group in old["groups"]:
        affected_files = sorted(changed & set(group["files"] + group["dependencies"]))
        affected_assumptions = sorted(changed_assumptions & set(group["assumptions"]))
        retained = not affected_files and not affected_assumptions
        if retained:
            covered.update(group["files"])
        else:
            reopened_files.update(group["files"])
        groups.append({"id": group["id"], "status": "retained" if retained else "reopened",
                       "changed_files": affected_files, "changed_assumptions": affected_assumptions})
    # Every material review dimension for a file must remain valid, even where
    # another group covers a different dimension of the same file.
    uncovered = sorted((set(scope) - covered) | (set(scope) & reopened_files))
    return {"groups": groups, "uncovered": uncovered, "coverage_complete": not uncovered,
            "semantic_quality": "not_assessed", "scope": scope}
