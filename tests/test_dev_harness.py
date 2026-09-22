import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import dev_harness


@pytest.fixture
def harness_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(dev_harness, "REPO_ROOT", repo.resolve())
    (repo / "src.txt").write_text("unembedded-source-sentinel-7a91\n", encoding="utf-8")
    (repo / "checks.log").write_text("1 passed\n", encoding="utf-8")
    return repo


def fingerprint(path: Path):
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest(), len(content)


def manifest():
    return {
        "version": 1,
        "task_id": "task-1",
        "objective": "Make the bounded change.",
        "task_class": "implementation",
        "allowed_edits": ["src.txt"],
        "inputs": [{"path": "src.txt", "reason": "Implementation target"}],
        "acceptance": ["Focused check passes"],
        "invariants": ["Do not touch production data"],
        "stop_conditions": ["Stop on ambiguous scope"],
    }


def result_for(repo: Path, packet: dict, *, status="completed"):
    source_hash, source_bytes = fingerprint(repo / "src.txt")
    log_hash, log_bytes = fingerprint(repo / "checks.log")
    return {
        "version": 1,
        "task_id": packet["task_id"],
        "packet_content_hash": packet["packet_content_hash"],
        "status": status,
        "summary": "Bounded change complete.",
        "outputs": [{"path": "src.txt", "sha256": source_hash, "bytes": source_bytes}],
        "acceptance_results": [
            {"acceptance": "Focused check passes", "status": "pass", "checks": ["focused"]}
        ],
        "validation_checks": [
            {
                "id": "focused",
                "description": "Focused offline test",
                "status": "pass",
                "log_path": "checks.log",
                "log_sha256": log_hash,
                "log_bytes": log_bytes,
            }
        ],
        "unresolved_findings": [],
        "usage": None,
        "cost_usd": None,
    }


def test_prepare_is_compact_deterministic_and_refuses_output_collision(harness_repo):
    manifest_path = harness_repo / "task.json"
    original = json.dumps(manifest(), indent=2).encode()
    manifest_path.write_bytes(original)
    output = harness_repo / "packet-dir"

    packet_path = dev_harness.prepare(manifest_path, output)
    packet = json.loads(packet_path.read_text())

    assert (output / "manifest.json").read_bytes() == original
    assert packet["routing"] == {
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "fork_turns": "none",
    }
    assert packet["inputs"][0]["path"] == "src.txt"
    assert "unembedded-source-sentinel-7a91" not in packet["prompt"]
    assert len(packet["prompt"]) <= 12_000
    dev_harness.verify_packet(packet)
    with pytest.raises(dev_harness.HarnessError, match="already exists"):
        dev_harness.prepare(manifest_path, output)


def test_prepare_rejects_path_escape_and_duplicate_paths(harness_repo):
    escaped = manifest()
    escaped["allowed_edits"] = ["../outside.txt"]
    with pytest.raises(dev_harness.HarnessError, match="repository-relative"):
        dev_harness.build_packet(escaped)

    duplicate = manifest()
    duplicate["inputs"].append(dict(duplicate["inputs"][0]))
    with pytest.raises(dev_harness.HarnessError, match="duplicate input"):
        dev_harness.build_packet(duplicate)


def test_prepare_rejects_symlink_escape(harness_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (harness_repo / "escape.txt").symlink_to(outside)
    task = manifest()
    task["inputs"] = [{"path": "escape.txt", "reason": "must remain inside"}]
    with pytest.raises(dev_harness.HarnessError, match="escapes repository"):
        dev_harness.build_packet(task)


def test_verify_detects_stale_input(harness_repo):
    packet = dev_harness.build_packet(manifest())
    (harness_repo / "src.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(dev_harness.HarnessError, match="stale or mismatched prepared input"):
        dev_harness.verify_packet(packet)


def test_verify_detects_changed_packet_and_rehashed_routing_policy(harness_repo):
    packet = dev_harness.build_packet(manifest())
    packet["objective"] = "quietly changed"
    with pytest.raises(dev_harness.HarnessError, match="content hash mismatch"):
        dev_harness.verify_packet(packet)

    packet = dev_harness.build_packet(manifest())
    packet["routing"]["model"] = "gpt-6-astra"
    packet["prompt"] = dev_harness._make_prompt(packet)
    packet["packet_content_hash"] = dev_harness._packet_hash(packet)
    with pytest.raises(dev_harness.HarnessError, match="routing does not match"):
        dev_harness.verify_packet(packet)


def test_oversize_prompt_fails_without_truncation(harness_repo):
    task = manifest()
    task["objective"] = "x" * 12_000
    with pytest.raises(dev_harness.HarnessError, match="refuse to truncate"):
        dev_harness.build_packet(task)


def test_result_accepts_declared_edit_but_checks_immutable_inputs(harness_repo):
    task = manifest()
    (harness_repo / "policy.txt").write_text("fixed\n", encoding="utf-8")
    task["inputs"].append({"path": "policy.txt", "reason": "Immutable policy"})
    packet = dev_harness.build_packet(task)
    (harness_repo / "src.txt").write_text("after\n", encoding="utf-8")
    result = result_for(harness_repo, packet)
    dev_harness.check_result(packet, result)

    (harness_repo / "policy.txt").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(dev_harness.HarnessError, match="prepared input"):
        dev_harness.check_result(packet, result)


def test_result_rejects_false_completion_and_missing_failed_check(harness_repo):
    packet = dev_harness.build_packet(manifest())
    result = result_for(harness_repo, packet)
    result["acceptance_results"] = []
    with pytest.raises(dev_harness.HarnessError, match="does not cover all acceptance"):
        dev_harness.check_result(packet, result)

    result = result_for(harness_repo, packet)
    result["acceptance_results"][0]["checks"] = ["missing"]
    with pytest.raises(dev_harness.HarnessError, match="references missing checks"):
        dev_harness.check_result(packet, result)

    result = result_for(harness_repo, packet)
    result["validation_checks"][0]["status"] = "fail"
    result["acceptance_results"][0]["status"] = "fail"
    with pytest.raises(dev_harness.HarnessError, match="acceptance item that did not pass"):
        dev_harness.check_result(packet, result)


def test_result_rejects_output_outside_allowed_edits(harness_repo):
    packet = dev_harness.build_packet(manifest())
    result = result_for(harness_repo, packet)
    digest, size = fingerprint(harness_repo / "checks.log")
    result["outputs"] = [{"path": "checks.log", "sha256": digest, "bytes": size}]
    with pytest.raises(dev_harness.HarnessError, match="outside allowed_edits"):
        dev_harness.check_result(packet, result)


def test_unknown_usage_and_cost_remain_null(harness_repo):
    packet = dev_harness.build_packet(manifest())
    (harness_repo / "src.txt").write_text("after\n", encoding="utf-8")
    result = result_for(harness_repo, packet)
    assert result["usage"] is None and result["cost_usd"] is None
    dev_harness.check_result(packet, result)


def test_cli_prepare_verify_and_check_first_packet(harness_repo, capsys):
    manifest_path = harness_repo / "task.json"
    manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
    packet_dir = harness_repo / "first-packet"
    assert dev_harness.main(["prepare", str(manifest_path), "--out", str(packet_dir)]) == 0
    packet_path = packet_dir / "packet.json"
    assert dev_harness.main(["verify", str(packet_path)]) == 0

    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    (harness_repo / "src.txt").write_text("after\n", encoding="utf-8")
    result_path = harness_repo / "result.json"
    result_path.write_text(json.dumps(result_for(harness_repo, packet)), encoding="utf-8")
    assert dev_harness.main(["check-result", str(packet_path), str(result_path)]) == 0
    output = capsys.readouterr().out
    assert '"review_required": true' in output


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_result_rejects_nonfinite_costs(harness_repo, value):
    packet = dev_harness.build_packet(manifest())
    result = result_for(harness_repo, packet)
    result["cost_usd"] = value
    with pytest.raises(dev_harness.HarnessError, match="finite non-negative"):
        dev_harness.check_result(packet, result)


@pytest.mark.parametrize("path", [".", "existing-dir"])
def test_edit_scope_requires_files(harness_repo, path):
    (harness_repo / "existing-dir").mkdir()
    task = manifest()
    task["allowed_edits"] = [path]
    with pytest.raises(dev_harness.HarnessError):
        dev_harness.build_packet(task)


def test_json_rejects_nonfinite_and_invalid_unicode(harness_repo):
    path = harness_repo / "invalid.json"
    path.write_text('{"cost_usd": NaN}')
    with pytest.raises(dev_harness.HarnessError, match="non-finite"):
        dev_harness._load_json(path)
    path.write_bytes(b"\xff")
    with pytest.raises(dev_harness.HarnessError, match="cannot read"):
        dev_harness._load_json(path)


def test_frozen_original_contract_remains_verifiable(harness_repo):
    packet = dev_harness.build_packet(manifest())
    assert packet["expected_result_contract"]["check_and_acceptance_statuses"] == ["pass", "fail", "not_run"]
    packet["expected_result_contract"] = dev_harness.RESULT_CONTRACT_V1
    packet["prompt"] = dev_harness._make_prompt(packet)
    packet["packet_content_hash"] = dev_harness._packet_hash(packet)
    dev_harness.verify_packet(packet)


def test_global_command_guidance_preserves_frozen_project_contract(harness_repo):
    packet = dev_harness.build_packet(manifest(), root=harness_repo)
    assert "~/.local/bin/codex-harness --root" in packet["expected_result_contract"]["instruction"]
    packet["expected_result_contract"] = dev_harness.RESULT_CONTRACT_PROJECT_COMMAND
    packet["prompt"] = dev_harness._make_prompt(packet)
    packet["packet_content_hash"] = dev_harness._packet_hash(packet)
    dev_harness.verify_packet(packet, root=harness_repo)


def test_explicit_root_cli_works_outside_workspace_and_binds_packet(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "src.txt").write_text("source\n", encoding="utf-8")
    (workspace / "task.json").write_text(json.dumps(manifest()), encoding="utf-8")
    helper = Path(dev_harness.__file__).resolve()

    prepared = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--root",
            str(workspace),
            "prepare",
            "task.json",
            "--out",
            "packet-dir",
        ],
        cwd=outside,
        text=True,
        capture_output=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    packet_path = workspace / "packet-dir" / "packet.json"
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["workspace_root"] == str(workspace.resolve())
    assert json.loads(prepared.stdout)["packet"] == "packet-dir/packet.json"

    verified = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--root",
            str(workspace),
            "verify",
            "packet-dir/packet.json",
        ],
        cwd=outside,
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr

    (workspace / "src.txt").write_text("changed\n", encoding="utf-8")
    (workspace / "checks.log").write_text("focused check passed\n", encoding="utf-8")
    (workspace / "result.json").write_text(
        json.dumps(result_for(workspace, packet)), encoding="utf-8"
    )
    checked = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--root",
            str(workspace),
            "check-result",
            "packet-dir/packet.json",
            "result.json",
        ],
        cwd=outside,
        text=True,
        capture_output=True,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["review_required"] is True


def test_explicit_root_rejects_cross_workspace_packet_and_cli_escape(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    outside = tmp_path / "outside"
    for directory in (first, second, outside):
        directory.mkdir()
    for workspace in (first, second):
        (workspace / "src.txt").write_text("source\n", encoding="utf-8")
    (first / "task.json").write_text(json.dumps(manifest()), encoding="utf-8")
    helper = Path(dev_harness.__file__).resolve()
    prepared = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--root",
            str(first),
            "prepare",
            "task.json",
            "--out",
            "packet-dir",
        ],
        cwd=outside,
        text=True,
        capture_output=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr
    (second / "packet.json").write_bytes((first / "packet-dir" / "packet.json").read_bytes())

    cross_workspace = subprocess.run(
        [sys.executable, str(helper), "--root", str(second), "verify", "packet.json"],
        cwd=outside,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cross_workspace.returncode == 2
    assert "bound to a different workspace" in cross_workspace.stderr

    (second / "result.json").write_text("{}", encoding="utf-8")
    cross_workspace_result = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--root",
            str(second),
            "check-result",
            "packet.json",
            "result.json",
        ],
        cwd=outside,
        text=True,
        capture_output=True,
        check=False,
    )
    assert cross_workspace_result.returncode == 2
    assert "bound to a different workspace" in cross_workspace_result.stderr

    escaped = subprocess.run(
        [sys.executable, str(helper), "--root", str(first), "verify", "../second/packet.json"],
        cwd=outside,
        text=True,
        capture_output=True,
        check=False,
    )
    assert escaped.returncode == 2
    assert "escapes repository" in escaped.stderr


def explicit_manifest():
    task = manifest()
    task.update(version=2, routing={
        "model": "gpt-6-astra", "reasoning_effort": "ultra",
        "reason": "User selected Astra for this implementation",
        "uncertainty": "Accounting identity needs careful review",
        "escalation_condition": "Conflicting source evidence",
        "policy_version": "local-v2", "selected_model": "gpt-6-astra",
        "selected_effort": "ultra",
    })
    return task


@pytest.mark.parametrize("task_class", ["implementation", "research"])
def test_v2_explicit_astra_ultra_is_request_not_observed_identity(harness_repo, task_class):
    task = explicit_manifest()
    task["task_class"] = task_class
    packet = dev_harness.build_packet(task)
    assert packet["version"] == 2
    assert packet["routing"]["model"] == "gpt-6-astra"
    assert packet["routing"]["reasoning_effort"] == "ultra"
    assert packet["routing"]["identity_kind"] == "requested"
    assert "observed_model" not in packet["routing"]
    dev_harness.verify_packet(packet)
    dev_harness.check_result(packet, result_for(harness_repo, packet))


@pytest.mark.parametrize("patch,match", [
    ({"model": "gpt-5.6-sol"}, "selected_model"),
    ({"reasoning_effort": "high"}, "selected_effort"),
    ({"model": "gpt-5.6-luna"}, "unavailable"),
    ({"model": "third-party"}, "unavailable"),
    ({"reasoning_effort": "invalid"}, "unavailable"),
    ({"reason": ""}, "non-empty"),
])
def test_v2_selection_and_route_validation(harness_repo, patch, match):
    task = explicit_manifest()
    task["routing"].update(patch)
    with pytest.raises(dev_harness.HarnessError, match=match):
        dev_harness.build_packet(task)


def test_v2_tampering_and_rehashed_constraint_mismatch(harness_repo):
    packet = dev_harness.build_packet(explicit_manifest())
    packet["routing"]["reasoning_effort"] = "high"
    with pytest.raises(dev_harness.HarnessError, match="hash mismatch"):
        dev_harness.verify_packet(packet)
    packet["prompt"] = dev_harness._make_prompt(packet)
    packet["packet_content_hash"] = dev_harness._packet_hash(packet)
    with pytest.raises(dev_harness.HarnessError, match="selected_effort"):
        dev_harness.verify_packet(packet)


def test_v2_route_required_and_v1_shape_remains_fixed(harness_repo):
    task = manifest()
    task["version"] = 2
    with pytest.raises(dev_harness.HarnessError, match="routing"):
        dev_harness.build_packet(task)
    task = explicit_manifest()
    task["version"] = 1
    with pytest.raises(dev_harness.HarnessError, match="unsupported fields"):
        dev_harness.build_packet(task)
