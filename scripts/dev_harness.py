#!/usr/bin/env python3
"""Prepare and validate compact, deterministic Atlas development task packets.

This helper is deliberately offline.  It records work for another process or person
to perform, but never dispatches a model or executes a validation command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKET_VERSION = 1
SUPPORTED_ROUTE_EFFORTS = {
    "gpt-6-astra": {"low", "medium", "high", "xhigh", "max", "ultra"},
    "gpt-5.6-sol": {"low", "medium", "high", "xhigh", "max", "ultra"},
}
MAX_PROMPT_CHARS = 12_000
TASK_ROUTING = {
    "implementation": "gpt-5.6-sol",
    "routine_review": "gpt-5.6-sol",
    "source_extract": "gpt-5.6-sol",
    "economic_reasoning": "gpt-6-astra",
    "architecture": "gpt-6-astra",
    "consequential_review": "gpt-6-astra",
}
CHECK_STATUSES = {"pass", "fail", "not_run"}
RESULT_STATUSES = {"completed", "needs_review", "blocked"}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

RESULT_CONTRACT_V1 = {
    "version": 1,
    "required_fields": [
        "version",
        "task_id",
        "packet_content_hash",
        "status",
        "summary",
        "outputs",
        "acceptance_results",
        "validation_checks",
        "unresolved_findings",
    ],
    "statuses": ["completed", "needs_review", "blocked"],
    "output_fields": ["path", "sha256", "bytes"],
    "acceptance_result_fields": ["acceptance", "status", "checks"],
    "validation_check_fields": [
        "id",
        "description",
        "status",
        "log_path",
        "log_sha256",
        "log_bytes",
    ],
    "completion_rule": (
        "Every acceptance item is present once, passes, references at least one "
        "passing validation check, every validation check passes, and unresolved_findings is empty."
    ),
    "evidence_limit": (
        "A retained log and matching hash prove only which evidence was supplied; "
        "they do not prove semantic quality. Independent lead review is still required."
    ),
    "usage_and_cost": "Optional; use null when unavailable and never infer zero.",
}

# Add explicit type/enum guidance without invalidating frozen packets prepared
# before the first pilot exposed ambiguous instructions. Validation is unchanged.
RESULT_CONTRACT_PROJECT_COMMAND = {
    **RESULT_CONTRACT_V1,
    "check_and_acceptance_statuses": ["pass", "fail", "not_run"],
    "unresolved_findings_type": "Array of non-empty strings; use [] when none.",
    "optional_fields": {"usage": "object or null", "cost_usd": "finite non-negative number or null"},
    "instruction": "usage_and_cost is explanatory metadata, not a result field. Validate with scripts/dev_harness.py check-result before reporting success.",
}

EXPECTED_RESULT_CONTRACT = {
    **RESULT_CONTRACT_PROJECT_COMMAND,
    "instruction": (
        "usage_and_cost is explanatory metadata, not a result field. Validate with "
        "~/.local/bin/codex-harness --root /absolute/project check-result <packet> <result> "
        "before reporting success. Use packet.workspace_root when present; otherwise "
        "use the explicitly assigned project. The project-local helper is an alternative "
        "only when actually installed there."
    ),
}


class HarnessError(ValueError):
    """A deterministic validation failure suitable for CLI display."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise HarnessError(f"cannot read {path}: {exc}") from exc
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_nonfinite)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HarnessError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"{path} must contain a JSON object")
    return value


def _reject_nonfinite(value: str) -> None:
    raise HarnessError(f"non-finite JSON number: {value}")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_fingerprint(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise HarnessError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest(), size


def _workspace_root(root: Path | None = None) -> Path:
    candidate = REPO_ROOT if root is None else root
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HarnessError(f"cannot resolve workspace root {candidate}: {exc}") from exc
    if not resolved.is_dir():
        raise HarnessError(f"workspace root is not a directory: {candidate}")
    return resolved


def _inside_repo(path: Path, *, must_exist: bool, label: str, root: Path | None = None) -> Path:
    repo_root = _workspace_root(root)
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise HarnessError(f"cannot resolve {label} {path}: {exc}") from exc
    try:
        resolved.relative_to(repo_root)
    except ValueError as exc:
        raise HarnessError(f"{label} escapes repository: {path}") from exc
    return resolved


def _repo_path(
    value: Any, *, must_exist: bool, label: str, root: Path | None = None
) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{label} must be a non-empty repository-relative path")
    if value != value.strip() or "\\" in value:
        raise HarnessError(f"{label} must be a normalized repository-relative path: {value!r}")
    pure = PurePosixPath(value)
    if not pure.parts or pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise HarnessError(f"{label} must be a normalized repository-relative path: {value!r}")
    normalized = pure.as_posix()
    if normalized != value:
        raise HarnessError(f"{label} must be normalized: {value!r}")
    repo_root = _workspace_root(root)
    resolved = _inside_repo(
        repo_root / normalized, must_exist=must_exist, label=label, root=repo_root
    )
    if (must_exist or resolved.exists()) and not resolved.is_file():
        raise HarnessError(f"{label} is not a regular file: {value}")
    return normalized, resolved


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessError(f"{label} must be a non-empty string")
    return value


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise HarnessError(f"{label} must be a non-empty list")
    result = [_nonempty_string(item, f"{label} item") for item in value]
    if len(result) != len(set(result)):
        raise HarnessError(f"{label} contains duplicates")
    return result


def _exact_fields(
    value: dict[str, Any], required: set[str], optional: set[str], label: str
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required - optional)
    if missing:
        raise HarnessError(f"{label} missing required fields: {', '.join(missing)}")
    if unknown:
        raise HarnessError(f"{label} has unsupported fields: {', '.join(unknown)}")


def _validate_manifest(
    manifest: dict[str, Any], *, root: Path | None = None
) -> dict[str, Any]:
    required = {
        "version",
        "task_id",
        "objective",
        "task_class",
        "allowed_edits",
        "inputs",
        "acceptance",
        "invariants",
        "stop_conditions",
    }
    version = manifest.get("version")
    if type(version) is not int or version not in {1, 2}:
        raise HarnessError("manifest version must be 1 or 2")
    if version == 2:
        required.add("routing")
    _exact_fields(manifest, required, {"max_attempts"}, "manifest")
    task_id = _nonempty_string(manifest["task_id"], "task_id")
    objective = _nonempty_string(manifest["objective"], "objective")
    task_class = manifest["task_class"]
    if task_class not in TASK_ROUTING and not (version == 2 and task_class == "research"):
        raise HarnessError(f"unsupported task_class: {task_class!r}")

    if not isinstance(manifest["allowed_edits"], list) or not manifest["allowed_edits"]:
        raise HarnessError("allowed_edits must be a non-empty list")
    allowed_edits: list[str] = []
    for index, item in enumerate(manifest["allowed_edits"]):
        normalized, _ = _repo_path(
            item, must_exist=False, label=f"allowed_edits[{index}]", root=root
        )
        allowed_edits.append(normalized)
    if len(allowed_edits) != len(set(allowed_edits)):
        raise HarnessError("allowed_edits contains duplicates")

    inputs_value = manifest["inputs"]
    if not isinstance(inputs_value, list) or not inputs_value:
        raise HarnessError("inputs must be a non-empty list")
    inputs: list[dict[str, str]] = []
    seen_inputs: set[str] = set()
    for index, item in enumerate(inputs_value):
        if not isinstance(item, dict):
            raise HarnessError(f"inputs[{index}] must be an object")
        _exact_fields(item, {"path", "reason"}, set(), f"inputs[{index}]")
        path, _ = _repo_path(
            item["path"], must_exist=True, label=f"inputs[{index}].path", root=root
        )
        if path in seen_inputs:
            raise HarnessError(f"duplicate input path: {path}")
        seen_inputs.add(path)
        inputs.append({"path": path, "reason": _nonempty_string(item["reason"], f"inputs[{index}].reason")})

    max_attempts = manifest.get("max_attempts", 2)
    if type(max_attempts) is not int or max_attempts < 1:
        raise HarnessError("max_attempts must be a positive integer")

    return {
        "version": version,
        **({"routing": _validate_route(manifest["routing"])} if version == 2 else {}),
        "task_id": task_id,
        "objective": objective,
        "task_class": task_class,
        "allowed_edits": allowed_edits,
        "inputs": inputs,
        "acceptance": _string_list(manifest["acceptance"], "acceptance"),
        "invariants": _string_list(manifest["invariants"], "invariants"),
        "stop_conditions": _string_list(manifest["stop_conditions"], "stop_conditions"),
        "max_attempts": max_attempts,
    }


def _routing(task_class: str) -> dict[str, str]:
    return {
        "model": TASK_ROUTING[task_class],
        "reasoning_effort": "high",
        "fork_turns": "none",
    }


def _validate_route(route: Any, *, packet: bool = False) -> dict[str, str]:
    """Validate a requested native route; this is not observed worker identity."""
    if not isinstance(route, dict):
        raise HarnessError("routing must be an object")
    required = {"model", "reasoning_effort", "reason", "uncertainty",
                "escalation_condition", "policy_version"}
    if packet:
        required.update({"fork_turns", "identity_kind"})
    _exact_fields(route, required, {"selected_model", "selected_effort"}, "routing")
    for key, value in route.items():
        _nonempty_string(value, f"routing.{key}")
    model, effort = route["model"], route["reasoning_effort"]
    if model not in SUPPORTED_ROUTE_EFFORTS or effort not in SUPPORTED_ROUTE_EFFORTS[model]:
        raise HarnessError(f"unavailable native route: {model}/{effort}")
    for selection, requested in (("selected_model", model), ("selected_effort", effort)):
        if selection in route and route[selection] != requested:
            raise HarnessError(f"routing does not match explicit {selection}")
    if packet and (route["fork_turns"] != "none" or route["identity_kind"] != "requested"):
        raise HarnessError("packet routing must use fresh context and requested identity")
    return dict(route)


def _make_prompt(packet: dict[str, Any]) -> str:
    prompt_keys = [
        "task_id",
        "objective",
        "task_class",
        "allowed_edits",
        "inputs",
        "acceptance",
        "invariants",
        "stop_conditions",
        "max_attempts",
        "routing",
        "expected_result_contract",
    ]
    if "workspace_root" in packet:
        prompt_keys.append("workspace_root")
    prompt_body = {
        key: packet[key]
        for key in prompt_keys
    }
    return (
        "Atlas development task packet. Work only within this explicit contract. "
        "Stop and escalate on a stop condition; do not broaden scope or lower acceptance.\n"
        + json.dumps(prompt_body, sort_keys=True, ensure_ascii=False, indent=2)
    )


def _packet_hash(packet: dict[str, Any]) -> str:
    content = dict(packet)
    content.pop("packet_content_hash", None)
    return _sha256_bytes(_canonical_bytes(content))


def build_packet(manifest: dict[str, Any], *, root: Path | None = None) -> dict[str, Any]:
    repo_root = _workspace_root(root)
    validated = _validate_manifest(manifest, root=repo_root)
    packet_inputs = []
    for item in validated["inputs"]:
        _, path = _repo_path(
            item["path"], must_exist=True, label="input path", root=repo_root
        )
        digest, size = _file_fingerprint(path)
        packet_inputs.append({**item, "sha256": digest, "bytes": size})
    packet = {
        "version": validated["version"],
        "kind": "atlas_development_task_packet",
        **{key: value for key, value in validated.items() if key != "version" and key != "inputs"},
        "inputs": packet_inputs,
        "routing": ({**validated["routing"], "fork_turns": "none", "identity_kind": "requested"}
                    if validated["version"] == 2 else _routing(validated["task_class"])),
        "expected_result_contract": EXPECTED_RESULT_CONTRACT,
    }
    if root is not None:
        packet["workspace_root"] = str(repo_root)
    packet["prompt"] = _make_prompt(packet)
    if len(packet["prompt"]) > MAX_PROMPT_CHARS:
        raise HarnessError(
            f"packet prompt is {len(packet['prompt'])} characters; maximum is {MAX_PROMPT_CHARS}; "
            "refuse to truncate"
        )
    packet["packet_content_hash"] = _packet_hash(packet)
    return packet


def prepare(
    manifest_path: Path, output_dir: Path, *, root: Path | None = None
) -> Path:
    repo_root = _workspace_root(root)
    manifest_resolved = _inside_repo(
        manifest_path, must_exist=True, label="manifest", root=repo_root
    )
    if not manifest_resolved.is_file():
        raise HarnessError(f"manifest is not a regular file: {manifest_path}")
    output_resolved = _inside_repo(
        output_dir, must_exist=False, label="output directory", root=repo_root
    )
    if output_resolved.exists() or output_dir.is_symlink():
        raise HarnessError(f"output path already exists: {output_dir}")
    try:
        original_manifest = manifest_resolved.read_bytes()
        manifest = json.loads(
            original_manifest.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except OSError as exc:
        raise HarnessError(f"cannot read {manifest_resolved}: {exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HarnessError(f"invalid JSON in {manifest_resolved}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise HarnessError(f"{manifest_resolved} must contain a JSON object")
    packet = build_packet(manifest, root=repo_root if root is not None else None)
    try:
        output_resolved.mkdir(parents=True, exist_ok=False)
        with (output_resolved / "manifest.json").open("xb") as stream:
            stream.write(original_manifest)
        with (output_resolved / "packet.json").open("x", encoding="utf-8") as stream:
            json.dump(packet, stream, sort_keys=True, ensure_ascii=False, indent=2)
            stream.write("\n")
    except FileExistsError as exc:
        raise HarnessError(f"refusing to overwrite packet output: {exc.filename}") from exc
    except OSError as exc:
        raise HarnessError(f"cannot write packet output: {exc}") from exc
    return output_resolved / "packet.json"


def _validate_packet_structure(
    packet: dict[str, Any], *, root: Path | None = None
) -> None:
    required = {
        "version",
        "kind",
        "task_id",
        "objective",
        "task_class",
        "allowed_edits",
        "inputs",
        "acceptance",
        "invariants",
        "stop_conditions",
        "max_attempts",
        "routing",
        "expected_result_contract",
        "prompt",
        "packet_content_hash",
    }
    _exact_fields(packet, required, {"workspace_root"}, "packet")
    if (type(packet["version"]) is not int or packet["version"] not in {1, 2}
            or packet["kind"] != "atlas_development_task_packet"):
        raise HarnessError("unsupported packet kind or version")
    if not isinstance(packet["packet_content_hash"], str) or not SHA256_RE.fullmatch(packet["packet_content_hash"]):
        raise HarnessError("packet_content_hash must be a lowercase SHA-256")
    if packet["packet_content_hash"] != _packet_hash(packet):
        raise HarnessError("packet content hash mismatch")
    if "workspace_root" in packet:
        workspace_root = _nonempty_string(packet["workspace_root"], "packet.workspace_root")
        if not Path(workspace_root).is_absolute():
            raise HarnessError("packet.workspace_root must be an absolute canonical path")
        if workspace_root != str(_workspace_root(root)):
            raise HarnessError(
                f"packet is bound to a different workspace: {workspace_root}"
            )
    if packet["task_class"] not in TASK_ROUTING and not (packet["version"] == 2 and packet["task_class"] == "research"):
        raise HarnessError(f"unsupported packet task_class: {packet['task_class']!r}")
    if packet["version"] == 1:
        if packet["routing"] != _routing(packet["task_class"]):
            raise HarnessError("packet routing does not match the fixed task-class policy")
    else:
        _validate_route(packet["routing"], packet=True)
    if packet["expected_result_contract"] not in (
        RESULT_CONTRACT_V1, RESULT_CONTRACT_PROJECT_COMMAND, EXPECTED_RESULT_CONTRACT
    ):
        raise HarnessError("packet result contract does not match current policy")
    if not isinstance(packet["prompt"], str) or len(packet["prompt"]) > MAX_PROMPT_CHARS:
        raise HarnessError("packet prompt is missing or exceeds 12000 characters")
    if packet["prompt"] != _make_prompt(packet):
        raise HarnessError("packet prompt does not match its structured task contract")
    # Reuse manifest validation for the shared task contract and then validate fingerprints.
    manifest_shape = {
        key: packet[key]
        for key in (
            "version",
            "task_id",
            "objective",
            "task_class",
            "allowed_edits",
            "acceptance",
            "invariants",
            "stop_conditions",
            "max_attempts",
        )
    }
    stripped_inputs = []
    for index, item in enumerate(packet["inputs"] if isinstance(packet["inputs"], list) else []):
        if not isinstance(item, dict):
            raise HarnessError(f"packet inputs[{index}] must be an object")
        _exact_fields(item, {"path", "reason", "sha256", "bytes"}, set(), f"packet inputs[{index}]")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise HarnessError(f"packet inputs[{index}].sha256 is invalid")
        if type(item["bytes"]) is not int or item["bytes"] < 0:
            raise HarnessError(f"packet inputs[{index}].bytes is invalid")
        stripped_inputs.append({"path": item["path"], "reason": item["reason"]})
    manifest_shape["inputs"] = stripped_inputs
    if packet["version"] == 2:
        manifest_shape["routing"] = {k: v for k, v in packet["routing"].items()
                                     if k not in {"fork_turns", "identity_kind"}}
    _validate_manifest(manifest_shape, root=_workspace_root(root))


def verify_packet(
    packet: dict[str, Any],
    editable_outputs: dict[str, tuple[str, int]] | None = None,
    *,
    root: Path | None = None,
) -> None:
    repo_root = _workspace_root(root)
    _validate_packet_structure(packet, root=repo_root)
    editable_outputs = editable_outputs or {}
    allowed = set(packet["allowed_edits"])
    for item in packet["inputs"]:
        path_text, path = _repo_path(
            item["path"], must_exist=True, label="packet input", root=repo_root
        )
        actual_hash, actual_bytes = _file_fingerprint(path)
        expected_hash = item["sha256"]
        expected_bytes = item["bytes"]
        if path_text in editable_outputs:
            if path_text not in allowed:
                raise HarnessError(f"modified input is outside allowed_edits: {path_text}")
            expected_hash, expected_bytes = editable_outputs[path_text]
        if (actual_hash, actual_bytes) != (expected_hash, expected_bytes):
            kind = "declared output" if path_text in editable_outputs else "prepared input"
            raise HarnessError(f"stale or mismatched {kind}: {path_text}")


def _validate_fingerprint_record(
    item: Any, label: str, path_field: str = "path", *, root: Path | None = None
) -> tuple[str, Path, str, int]:
    if not isinstance(item, dict):
        raise HarnessError(f"{label} must be an object")
    path_text, path = _repo_path(
        item[path_field], must_exist=True, label=f"{label}.{path_field}", root=root
    )
    digest_field = "log_sha256" if path_field == "log_path" else "sha256"
    bytes_field = "log_bytes" if path_field == "log_path" else "bytes"
    digest = item[digest_field]
    size = item[bytes_field]
    if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise HarnessError(f"{label}.{digest_field} must be a lowercase SHA-256")
    if type(size) is not int or size < 0:
        raise HarnessError(f"{label}.{bytes_field} must be a non-negative integer")
    actual = _file_fingerprint(path)
    if actual != (digest, size):
        raise HarnessError(f"{label} fingerprint mismatch: {path_text}")
    return path_text, path, digest, size


def check_result(
    packet: dict[str, Any], result: dict[str, Any], *, root: Path | None = None
) -> None:
    repo_root = _workspace_root(root)
    _validate_packet_structure(packet, root=repo_root)
    required = set(EXPECTED_RESULT_CONTRACT["required_fields"])
    _exact_fields(result, required, {"usage", "cost_usd"}, "result")
    if type(result["version"]) is not int or result["version"] != 1:
        raise HarnessError("result version must be 1")
    if result["task_id"] != packet.get("task_id"):
        raise HarnessError("result task_id does not match packet")
    if result["packet_content_hash"] != packet.get("packet_content_hash"):
        raise HarnessError("result packet_content_hash does not match packet")
    if result["status"] not in RESULT_STATUSES:
        raise HarnessError(f"unsupported result status: {result['status']!r}")
    _nonempty_string(result["summary"], "result.summary")

    outputs_value = result["outputs"]
    if not isinstance(outputs_value, list):
        raise HarnessError("result.outputs must be a list")
    allowed = set(packet.get("allowed_edits", []))
    outputs: dict[str, tuple[str, int]] = {}
    for index, item in enumerate(outputs_value):
        if not isinstance(item, dict):
            raise HarnessError(f"outputs[{index}] must be an object")
        _exact_fields(item, {"path", "sha256", "bytes"}, set(), f"outputs[{index}]")
        path_text, _, digest, size = _validate_fingerprint_record(
            item, f"outputs[{index}]", root=repo_root
        )
        if path_text not in allowed:
            raise HarnessError(f"output is outside allowed_edits: {path_text}")
        if path_text in outputs:
            raise HarnessError(f"duplicate output path: {path_text}")
        outputs[path_text] = (digest, size)

    # Validate immutable inputs against preparation and editable inputs against declared output.
    verify_packet(packet, editable_outputs=outputs, root=repo_root)

    checks_value = result["validation_checks"]
    if not isinstance(checks_value, list):
        raise HarnessError("result.validation_checks must be a list")
    checks: dict[str, str] = {}
    for index, item in enumerate(checks_value):
        if not isinstance(item, dict):
            raise HarnessError(f"validation_checks[{index}] must be an object")
        _exact_fields(
            item,
            {"id", "description", "status", "log_path", "log_sha256", "log_bytes"},
            set(),
            f"validation_checks[{index}]",
        )
        check_id = _nonempty_string(item["id"], f"validation_checks[{index}].id")
        _nonempty_string(item["description"], f"validation_checks[{index}].description")
        if check_id in checks:
            raise HarnessError(f"duplicate validation check id: {check_id}")
        if item["status"] not in CHECK_STATUSES:
            raise HarnessError(f"invalid validation check status for {check_id}")
        _validate_fingerprint_record(
            item,
            f"validation_checks[{index}]",
            path_field="log_path",
            root=repo_root,
        )
        checks[check_id] = item["status"]

    acceptance_value = result["acceptance_results"]
    if not isinstance(acceptance_value, list):
        raise HarnessError("result.acceptance_results must be a list")
    acceptance_results: dict[str, tuple[str, list[str]]] = {}
    packet_acceptance = set(packet.get("acceptance", []))
    for index, item in enumerate(acceptance_value):
        if not isinstance(item, dict):
            raise HarnessError(f"acceptance_results[{index}] must be an object")
        _exact_fields(item, {"acceptance", "status", "checks"}, set(), f"acceptance_results[{index}]")
        acceptance = _nonempty_string(item["acceptance"], f"acceptance_results[{index}].acceptance")
        if acceptance not in packet_acceptance:
            raise HarnessError(f"unknown acceptance item in result: {acceptance}")
        if acceptance in acceptance_results:
            raise HarnessError(f"duplicate acceptance result: {acceptance}")
        if item["status"] not in CHECK_STATUSES:
            raise HarnessError(f"invalid acceptance status for {acceptance}")
        references = _string_list(item["checks"], f"acceptance result checks for {acceptance}", allow_empty=True)
        missing_checks = sorted(set(references) - checks.keys())
        if missing_checks:
            raise HarnessError(
                f"acceptance item {acceptance!r} references missing checks: {', '.join(missing_checks)}"
            )
        if item["status"] == "pass" and (not references or any(checks[ref] != "pass" for ref in references)):
            raise HarnessError(f"passing acceptance item lacks only passing check evidence: {acceptance}")
        acceptance_results[acceptance] = (item["status"], references)

    findings = _string_list(result["unresolved_findings"], "unresolved_findings", allow_empty=True)
    for optional in ("usage", "cost_usd"):
        if optional in result and result[optional] is not None:
            if optional == "usage":
                if not isinstance(result[optional], dict):
                    raise HarnessError("usage must be an object or null")
            elif (isinstance(result[optional], bool) or not isinstance(result[optional], (int, float))
                  or result[optional] < 0 or not math.isfinite(result[optional])):
                raise HarnessError("cost_usd must be a finite non-negative number or null")

    if result["status"] == "completed":
        if set(acceptance_results) != packet_acceptance:
            missing = sorted(packet_acceptance - acceptance_results.keys())
            raise HarnessError(f"completed result does not cover all acceptance items: {', '.join(missing)}")
        if any(status != "pass" for status, _ in acceptance_results.values()):
            raise HarnessError("completed result contains an acceptance item that did not pass")
        if any(status != "pass" for status in checks.values()):
            raise HarnessError("completed result contains a failed or not-run validation check")
        if findings:
            raise HarnessError("completed result has unresolved findings")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        help="canonical workspace root; relative CLI paths are resolved from this directory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="create an immutable task packet directory")
    prepare_parser.add_argument("manifest", type=Path)
    prepare_parser.add_argument("--out", required=True, type=Path)
    verify_parser = subparsers.add_parser("verify", help="verify a packet and its prepared inputs")
    verify_parser.add_argument("packet", type=Path)
    result_parser = subparsers.add_parser("check-result", help="validate a result against its packet")
    result_parser.add_argument("packet", type=Path)
    result_parser.add_argument("result", type=Path)
    usage_parser = subparsers.add_parser("usage-manifest", help="summarize explicitly supplied attempts and agents")
    usage_parser.add_argument("manifest", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        repo_root = _workspace_root(args.root)

        def cli_path(path: Path) -> Path:
            return repo_root / path if args.root is not None and not path.is_absolute() else path

        if args.command == "usage-manifest":
            try:
                from .dev_harness_usage import summarize_manifest
            except ImportError:
                from dev_harness_usage import summarize_manifest
            response = summarize_manifest(cli_path(args.manifest), repo_root)
        elif args.command == "prepare":
            packet_path = prepare(
                cli_path(args.manifest),
                cli_path(args.out),
                root=repo_root if args.root is not None else None,
            )
            response = {"ok": True, "packet": str(packet_path.relative_to(repo_root))}
        elif args.command == "verify":
            packet_path = _inside_repo(
                cli_path(args.packet), must_exist=True, label="packet", root=repo_root
            )
            packet = _load_json(packet_path)
            verify_packet(packet, root=repo_root)
            response = {"ok": True, "task_id": packet["task_id"], "packet_content_hash": packet["packet_content_hash"]}
        else:
            packet_path = _inside_repo(
                cli_path(args.packet), must_exist=True, label="packet", root=repo_root
            )
            result_path = _inside_repo(
                cli_path(args.result), must_exist=True, label="result", root=repo_root
            )
            packet = _load_json(packet_path)
            result = _load_json(result_path)
            check_result(packet, result, root=repo_root)
            response = {
                "ok": True,
                "task_id": packet["task_id"],
                "status": result["status"],
                "review_required": True,
                "note": EXPECTED_RESULT_CONTRACT["evidence_limit"],
            }
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(response, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
