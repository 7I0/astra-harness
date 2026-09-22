import json

import pytest

from scripts.dev_harness_usage import summarize


def record(response="one", thread="root", **usage):
    return {"type": "token_usage_record", "timestamp": "2026-09-21T00:00:00Z",
            "payload": {"thread_id": thread, "response_id": response,
                        "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20, **usage},
                        "thread_token_usage": {"input_tokens": 999999999}}}


def save(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_deduplicate_without_cumulative_or_child_double_count(tmp_path):
    file = save(tmp_path / "log", [record(), record(), record("two"), record("child", "child")])
    result = summarize([file], "root")
    assert result["unique_responses"] == 2 and result["deduplicated_records"] == 1
    assert result["totals"]["input_tokens"] == 200
    assert result["totals"]["cached_input_tokens"] == 160
    assert result["totals"]["output_tokens"] == 40
    assert result["totals"]["cache_write_input_tokens"] is None
    assert result["cost_usd"] is None


def test_conflicting_duplicate_fails_instead_of_picking_one(tmp_path):
    file = save(tmp_path / "log", [record(), record(input_tokens=101)])
    with pytest.raises(ValueError, match="conflicting"):
        summarize([file], "root")


def test_partial_and_empty_coverage_stays_unknown(tmp_path):
    file = save(tmp_path / "log", [record(), record("two", output_tokens=None)])
    result = summarize([file], "root")
    assert result["totals"]["output_tokens"] is None
    assert result["coverage_responses"]["output_tokens"] == 1
    empty = summarize([file], "absent")
    assert all(v is None for v in empty["totals"].values())


def test_live_partial_line_and_text_are_not_exposed(tmp_path):
    file = save(tmp_path / "log", [record(), {"type": "response_item", "payload": {"text": "private-message"}}])
    with file.open("a") as stream:
        stream.write('{"partial":')
    result = summarize([file], "root")
    assert result["unique_responses"] == 1
    assert result["incomplete_final_records"] == 1
    assert "private-message" not in json.dumps(result)


def test_corrupt_complete_record_cannot_silently_reduce_usage(tmp_path):
    file = save(tmp_path / "log", [record()])
    with file.open("a") as stream:
        stream.write('{broken}\n')
        stream.write(json.dumps(record("two")) + "\n")
    with pytest.raises(ValueError, match="malformed telemetry"):
        summarize([file], "root")


@pytest.mark.parametrize("usage", [{"cached_input_tokens": 101}, {"reasoning_output_tokens": 21}])
def test_token_subsets_cannot_exceed_total(tmp_path, usage):
    file = save(tmp_path / "log", [record(**usage)])
    with pytest.raises(ValueError, match="invalid token subset"):
        summarize([file], "root")


def test_contradictory_total_rejected_but_partial_total_stays_unknown(tmp_path):
    file = save(tmp_path / "log", [record(total_tokens=999)])
    with pytest.raises(ValueError, match="inconsistent total_tokens"):
        summarize([file], "root")
    save(file, [record(total_tokens=120), record("two", total_tokens=None)])
    result = summarize([file], "root")
    assert result["totals"]["total_tokens"] is None
    assert result["coverage_responses"]["total_tokens"] == 1


from scripts.dev_harness_usage import summarize_manifest


def agent(thread, files=None, **changes):
    return {"thread_id": thread, "role": "lead" if thread == "root" else "worker",
            "status": "completed", "requested_model": "gpt-6-astra", "requested_effort": "ultra",
            "telemetry_files": files if files is not None else [thread + ".jsonl"],
            "coverage_complete": True, **changes}


def accounting(root, agents, **changes):
    value = {"version": 1, "task_id": "accepted-task", "coverage_complete": True,
             "attempts": [{"attempt_id": "attempt-1", "status": "completed",
                           "coverage_complete": True, "agents": agents}], **changes}
    path = root / "manifest.json"
    path.write_text(json.dumps(value))
    return path


def full_record(response, thread):
    return record(response, thread, cache_write_input_tokens=0, reasoning_output_tokens=5, total_tokens=120)


def test_manifest_counts_lead_children_review_repair_and_deduplicates(tmp_path):
    agents = [agent("root"), agent("child"), agent("review", role="reviewer")]
    for a in agents:
        save(tmp_path / a["telemetry_files"][0], [full_record(a["thread_id"], a["thread_id"])] * 2)
    path = accounting(tmp_path, agents)
    value = json.loads(path.read_text())
    value["attempts"].append({"attempt_id": "repair", "status": "completed", "coverage_complete": True,
                              "agents": [agent("repair")]})
    save(tmp_path / "repair.jsonl", [full_record("repair-response", "repair")])
    path.write_text(json.dumps(value))
    report = summarize_manifest(path, tmp_path)
    assert report["coverage_complete"] is True
    assert report["totals"]["total_tokens"] == 480
    assert report["totals"]["input_tokens"] == 400
    assert report["totals"]["cached_input_tokens"] == 320
    assert report["totals"]["output_tokens"] == 80
    assert report["totals"]["reasoning_output_tokens"] == 20
    assert report["cost_usd"] is None
    assert report["attempts"][0]["agents"][0]["observed_identity"] is None


@pytest.mark.parametrize("gap", ["missing_child", "empty_child", "partial_tail", "unattested", "active"])
def test_manifest_gap_invalidates_totals_retains_subtotals(tmp_path, gap):
    save(tmp_path / "root.jsonl", [full_record("r", "root")])
    agents = [agent("root")]
    if gap == "missing_child":
        agents.append(agent("child"))
    elif gap == "empty_child":
        save(tmp_path / "child.jsonl", [{"type": "response_item", "payload": {"text": "unexported-conversation-sentinel-8f19"}}])
        agents.append(agent("child"))
    elif gap == "partial_tail":
        with (tmp_path / "root.jsonl").open("a") as stream:
            stream.write('{"partial":')
    elif gap == "unattested":
        agents[0]["coverage_complete"] = False
    else:
        agents[0]["status"] = "active"
    report = summarize_manifest(accounting(tmp_path, agents), tmp_path)
    assert not report["coverage_complete"]
    assert all(v is None for v in report["totals"].values())
    assert report["observed_subtotals"]["total_tokens"] == 120
    assert "unexported-conversation-sentinel-8f19" not in json.dumps(report)


def test_manifest_missing_counters_stay_unknown(tmp_path):
    save(tmp_path / "root.jsonl", [record()])
    report = summarize_manifest(accounting(tmp_path, [agent("root")]), tmp_path)
    assert report["totals"]["total_tokens"] is None
    assert report["totals"]["input_tokens"] == 100
    assert report["observed_subtotals"]["total_tokens"] is None


@pytest.mark.parametrize("status", ["cancelled", "interrupted"])
def test_manifest_interrupted_usage_retained_cost_never_zero(tmp_path, status):
    save(tmp_path / "root.jsonl", [full_record("r", "root")])
    report = summarize_manifest(accounting(tmp_path, [agent("root", status=status)]), tmp_path)
    assert report["totals"]["total_tokens"] == 120
    assert report["cost_usd"] is None
    assert report["attempts"][0]["agents"][0]["status"] == status


def test_manifest_model_changes_do_not_inherit_last_identity(tmp_path):
    first, last = full_record("r1", "root"), full_record("r2", "root")
    first["payload"]["model"] = "gpt-6-astra"
    last["payload"]["model"] = "gpt-5.6-sol"
    save(tmp_path / "root.jsonl", [first, last])
    report = summarize_manifest(accounting(tmp_path, [agent("root")]), tmp_path)
    assert report["attempts"][0]["agents"][0]["observed_identity"] is None
    assert report["totals"]["total_tokens"] == 240


def test_manifest_same_thread_across_attempts_rejected(tmp_path):
    save(tmp_path / "root.jsonl", [full_record("r", "root")])
    path = accounting(tmp_path, [agent("root")])
    value = json.loads(path.read_text())
    value["attempts"].append({**value["attempts"][0], "attempt_id": "retry"})
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="thread_id overlaps"):
        summarize_manifest(path, tmp_path)


def test_manifest_same_response_across_threads_rejected(tmp_path):
    for thread in ["root", "child"]:
        save(tmp_path / (thread + ".jsonl"), [full_record("same-response", thread)])
    with pytest.raises(ValueError, match="response identity overlaps"):
        summarize_manifest(accounting(tmp_path, [agent("root"), agent("child")]), tmp_path)


def test_manifest_duplicate_attempt_and_boolean_attestation_validation(tmp_path):
    path = accounting(tmp_path, [])
    value = json.loads(path.read_text())
    value["attempts"].append(value["attempts"][0])
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="duplicate attempt_id"):
        summarize_manifest(path, tmp_path)
    path = accounting(tmp_path, [], coverage_complete="yes")
    with pytest.raises(ValueError, match="must be boolean"):
        summarize_manifest(path, tmp_path)


def test_manifest_cli_matches_api(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    save(tmp_path / "root.jsonl", [full_record("r", "root")])
    path = accounting(tmp_path, [agent("root")])
    helper = Path(__file__).resolve().parents[1] / "scripts" / "dev_harness.py"
    result = subprocess.run([sys.executable, str(helper), "--root", str(tmp_path),
                             "usage-manifest", path.name], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == summarize_manifest(path, tmp_path)
