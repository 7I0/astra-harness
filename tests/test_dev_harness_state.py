import copy
import hashlib
import multiprocessing
from pathlib import Path

import pytest

from scripts import dev_harness as h
from scripts import dev_harness_state as s


def record(root, path="observed.txt"):
    data = (root / path).read_bytes()
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


@pytest.fixture
def task(tmp_path):
    (tmp_path / "source.txt").write_text("source")
    (tmp_path / "observed.txt").write_text("observed actual files and worker outcome")
    packet = h.build_packet({"version": 1, "task_id": "state-task", "objective": "bounded change",
                             "task_class": "architecture", "allowed_edits": ["source.txt"],
                             "inputs": [{"path": "source.txt", "reason": "source"}],
                             "acceptance": ["valid"], "invariants": ["offline"],
                             "stop_conditions": ["ambiguity"], "max_attempts": 2}, root=tmp_path)
    manifest = {"phase": "implementation", "evidence": [], "active_workers": [],
                "attempts_used": 1, "next_action": "inspect result", "state": "active", "unresolved_mutations": []}
    return tmp_path, packet, manifest


def proposal(value, **changes):
    return dict(copy.deepcopy(value), **changes)


def test_init_and_no_overwrite_or_stale_write(task):
    root, packet, manifest = task
    initial = s.checkpoint_init(root, "run.json", packet, manifest)
    assert initial["revision"] == 0
    before = (root / "run.json").read_bytes()
    with pytest.raises(h.HarnessError, match="already exists"):
        s.checkpoint_init(root, "run.json", packet, manifest)
    with pytest.raises(h.HarnessError, match="stale checkpoint revision"):
        s.checkpoint_update(root, "run.json", packet, manifest, expected_revision=1)
    assert (root / "run.json").read_bytes() == before


def _cas_worker(root, packet, manifest, barrier, queue):
    barrier.wait(timeout=10)
    try:
        state = s.checkpoint_update(Path(root), "run.json", packet, manifest, expected_revision=0)
        queue.put(("pass", state["revision"]))
    except h.HarnessError as exc:
        queue.put(("fail", str(exc)))


def test_real_concurrent_cas_exactly_one_winner(task):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(2), context.Queue()
    workers = [context.Process(target=_cas_worker, args=(str(root), packet, manifest, barrier, queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    outcomes = [queue.get(timeout=20) for _ in workers]
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert sorted(item[0] for item in outcomes) == ["fail", "pass"]
    assert ("pass", 1) in outcomes
    assert any("stale checkpoint revision" in str(item) for item in outcomes)
    assert s.checkpoint_inspect(root, ["run.json"])["candidates"][0]["checkpoint"]["revision"] == 1


def test_interruption_after_replace_requires_inspection_not_replay(task, monkeypatch):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    original = s.os.replace
    def replaced_then_interrupted(source, destination):
        original(source, destination)
        raise OSError("connection lost after write")
    monkeypatch.setattr(s.os, "replace", replaced_then_interrupted)
    with pytest.raises(h.HarnessError, match="connection lost"):
        s.checkpoint_update(root, "run.json", packet, proposal(manifest, attempts_used=2), expected_revision=0)
    recovered = s.checkpoint_inspect(root, ["run.json"])["candidates"][0]["checkpoint"]
    assert recovered["revision"] == 1 and recovered["attempts_used"] == 2
    with pytest.raises(h.HarnessError, match="stale checkpoint revision"):
        s.checkpoint_update(root, "run.json", packet, manifest, expected_revision=0)


def test_interruption_before_replace_retains_prior_state(task, monkeypatch):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    before = (root / "run.json").read_bytes()
    def interrupted(*args):
        raise OSError("before replacement")
    monkeypatch.setattr(s.os, "replace", interrupted)
    with pytest.raises(h.HarnessError, match="before replacement"):
        s.checkpoint_update(root, "run.json", packet, manifest, expected_revision=0)
    assert (root / "run.json").read_bytes() == before


@pytest.mark.parametrize("missing", [False, True])
def test_source_drift_allows_only_quarantine_without_rebinding(task, missing):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    if missing:
        (root / "source.txt").unlink()
    else:
        (root / "source.txt").write_text("changed")
    before = (root / "run.json").read_bytes()
    with pytest.raises(h.HarnessError, match="quarantine required"):
        s.checkpoint_update(root, "run.json", packet, manifest, expected_revision=0)
    assert (root / "run.json").read_bytes() == before
    state = s.checkpoint_update(root, "run.json", packet, proposal(manifest, state="replan_required"), expected_revision=0)
    assert state["packet_content_hash"] == packet["packet_content_hash"]
    assert state["revision"] == 1


def test_contract_drift_does_not_rebind_old_checkpoint(task):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    changed = copy.deepcopy(packet)
    changed["objective"] = "new contract"
    changed["prompt"] = h._make_prompt(changed)
    changed["packet_content_hash"] = h._packet_hash(changed)
    with pytest.raises(h.HarnessError, match="quarantine required"):
        s.checkpoint_update(root, "run.json", changed, manifest, expected_revision=0)
    state = s.checkpoint_update(root, "run.json", changed, proposal(manifest, state="replan_required"), expected_revision=0)
    assert state["packet_content_hash"] == packet["packet_content_hash"]


@pytest.mark.parametrize("attempts", [0, 3, True])
def test_attempts_never_reset_or_exceed_budget(task, attempts):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    before = (root / "run.json").read_bytes()
    with pytest.raises(h.HarnessError):
        s.checkpoint_update(root, "run.json", packet, proposal(manifest, attempts_used=attempts), expected_revision=0)
    assert (root / "run.json").read_bytes() == before


def test_unknown_mutation_requires_observed_evidence_and_persists_it(task):
    root, packet, manifest = task
    unknown = proposal(manifest, state="reconciliation_required", unresolved_mutations=["write-1"])
    s.checkpoint_init(root, "run.json", packet, unknown)
    with pytest.raises(h.HarnessError, match="observed evidence required"):
        s.checkpoint_update(root, "run.json", packet, manifest, expected_revision=0)
    with pytest.raises(h.HarnessError, match="unknown mutation outcome"):
        s.checkpoint_update(root, "run.json", packet, proposal(unknown, state="active"), expected_revision=0)
    observed = record(root)
    resumed = s.checkpoint_update(root, "run.json", packet,
        proposal(manifest, reconciled=[{"mutation": "write-1", "evidence": [observed]}]), expected_revision=0)
    assert resumed["evidence"] == [observed]
    assert resumed["state"] == "active"


def test_worker_cannot_disappear_or_complete_without_observations(task):
    root, packet, manifest = task
    working = proposal(manifest, active_workers=["worker-1"])
    s.checkpoint_init(root, "run.json", packet, working)
    assert s.checkpoint_inspect(root, ["run.json"])["candidates"][0]["reconciliation_required"]
    with pytest.raises(h.HarnessError, match="workers prevent completion"):
        s.checkpoint_update(root, "run.json", packet, proposal(working, state="completed"), expected_revision=0)
    with pytest.raises(h.HarnessError, match="observed evidence required"):
        s.checkpoint_update(root, "run.json", packet, proposal(manifest, state="completed"), expected_revision=0)
    complete = s.checkpoint_update(root, "run.json", packet,
        proposal(manifest, state="completed", resolved_workers=[{"worker": "worker-1", "evidence": [record(root)]}]), expected_revision=0)
    assert complete["state"] == "completed"
    assert s.checkpoint_inspect(root, ["run.json"])["acceptance"] == "not_assessed"


def test_stale_evidence_can_be_quarantined(task):
    root, packet, manifest = task
    manifest["evidence"] = [record(root)]
    s.checkpoint_init(root, "run.json", packet, manifest)
    (root / "observed.txt").write_text("mutated")
    with pytest.raises(h.HarnessError, match="quarantine required"):
        s.checkpoint_update(root, "run.json", packet, manifest, expected_revision=0)
    state = s.checkpoint_update(root, "run.json", packet, proposal(manifest, state="replan_required"), expected_revision=0)
    assert state["evidence"] == manifest["evidence"]


def test_recovery_ambiguity_is_explicit(task):
    root, packet, manifest = task
    s.checkpoint_init(root, "one.json", packet, manifest)
    s.checkpoint_init(root, "two.json", packet, manifest)
    report = s.checkpoint_inspect(root, ["one.json", "two.json"])
    assert report["status"] == "ambiguous" and len(report["candidates"]) == 2


def test_symlink_escapes_rejected_for_state_lock_evidence(task, tmp_path_factory):
    root, packet, manifest = task
    outside = tmp_path_factory.mktemp("outside")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(h.HarnessError, match="escapes repository"):
        s.checkpoint_init(root, "escape/state.json", packet, manifest)
    (root / "run.json.lock").symlink_to(outside / "lock")
    with pytest.raises(h.HarnessError, match="escapes repository"):
        s.checkpoint_init(root, "run.json", packet, manifest)
    (outside / "evidence").write_text("x")
    with pytest.raises(h.HarnessError, match="escapes repository"):
        s.checkpoint_init(root, "evidence-state.json", packet, proposal(manifest, evidence=[record(root, "escape/evidence")]))
    assert list(outside.iterdir()) == [outside / "evidence"]


@pytest.fixture
def review(tmp_path):
    for name in ("a.py", "b.py", "c.py", "fixture.py", "untracked.py"):
        (tmp_path / name).write_text(name)
    assumptions = {"oracle": "strict", "independent": "unchanged"}
    manifest = {"scope": ["a.py", "b.py", "c.py", "untracked.py"], "assumptions": assumptions,
        "groups": [{"id": "a", "files": ["a.py"], "dependencies": ["fixture.py"], "assumptions": ["oracle"]},
                   {"id": "b", "files": ["b.py"], "dependencies": ["fixture.py"], "assumptions": ["oracle"]},
                   {"id": "independent", "files": ["c.py", "untracked.py"], "dependencies": [], "assumptions": ["independent"]}]}
    return tmp_path, s.review_snapshot(tmp_path, manifest), {"scope": manifest["scope"], "assumptions": assumptions}


@pytest.mark.parametrize("change", ["fixture", "assumption", "missing"])
def test_review_shared_dependency_reopens_both_but_retains_independent(review, change):
    root, snapshot, manifest = review
    if change == "fixture":
        (root / "fixture.py").write_text("weaker oracle")
    elif change == "missing":
        (root / "fixture.py").unlink()
    else:
        manifest["assumptions"]["oracle"] = "weaker"
    result = s.review_check(root, snapshot, manifest)
    assert [group["status"] for group in result["groups"]] == ["reopened", "reopened", "retained"]
    assert result["uncovered"] == ["a.py", "b.py"]
    assert result["semantic_quality"] == "not_assessed"


def test_review_new_intended_scope_never_inherits_approval(review):
    root, snapshot, manifest = review
    assert s.review_check(root, snapshot, manifest)["coverage_complete"]
    manifest["scope"].extend(["new.py", "not_created.py"])
    (root / "new.py").write_text("new scope")
    result = s.review_check(root, snapshot, manifest)
    assert result["uncovered"] == ["new.py", "not_created.py"]
    assert not result["coverage_complete"]


def test_review_tracks_untracked_changes_and_snapshot_tamper(review):
    root, snapshot, manifest = review
    (root / "untracked.py").write_text("unreviewed edit")
    result = s.review_check(root, snapshot, manifest)
    assert result["groups"][-1]["status"] == "reopened"
    snapshot["groups"][0]["files"].append("untracked.py")
    with pytest.raises(h.HarnessError, match="content hash mismatch"):
        s.review_check(root, snapshot, manifest)


def test_review_escape_rejected(review, tmp_path_factory):
    root, snapshot, manifest = review
    outside = tmp_path_factory.mktemp("review-outside")
    (outside / "source").write_text("x")
    (root / "a.py").unlink()
    (root / "a.py").symlink_to(outside / "source")
    with pytest.raises(h.HarnessError, match="escapes repository"):
        s.review_check(root, snapshot, manifest)


def test_reconciliation_cannot_omit_unknown_operation_identity(task):
    root, packet, manifest = task
    with pytest.raises(h.HarnessError, match="unresolved mutation identity"):
        s.checkpoint_init(root, "run.json", packet, proposal(manifest, state="reconciliation_required"))


def test_inspect_reports_stale_evidence_without_packet(task):
    root, packet, manifest = task
    manifest["evidence"] = [record(root)]
    s.checkpoint_init(root, "run.json", packet, manifest)
    (root / "observed.txt").unlink()
    assert s.checkpoint_inspect(root, ["run.json"])["candidates"][0]["drift"] == ["prior evidence changed: observed.txt"]


def test_invalid_review_schema_rejected_even_with_matching_hash(review):
    root, snapshot, manifest = review
    unsealed = s._unseal(snapshot)
    unsealed["groups"][0]["files"].append("outside_scope.py")
    with pytest.raises(h.HarnessError, match="outside declared scope"):
        s.review_check(root, s._seal(unsealed), manifest)


def test_replan_is_terminal_for_old_packet(task):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, proposal(manifest, state="replan_required"))
    with pytest.raises(h.HarnessError, match="replan requires a new packet"):
        s.checkpoint_update(root, "run.json", packet, manifest, expected_revision=0)


def test_replan_requirement_survives_reconciliation(task):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, proposal(manifest, state="replan_required"))
    unknown = proposal(manifest, state="reconciliation_required", unresolved_mutations=["write"])
    state = s.checkpoint_update(root, "run.json", packet, unknown, expected_revision=0)
    assert state["requires_new_packet"]
    observed = [{"mutation": "write", "evidence": [record(root)]}]
    for target in ("active", "completed"):
        with pytest.raises(h.HarnessError, match="replan requires a new packet"):
            s.checkpoint_update(root, "run.json", packet,
                proposal(manifest, state=target, reconciled=observed), expected_revision=1)
    state = s.checkpoint_update(root, "run.json", packet,
        proposal(manifest, state="replan_required", reconciled=observed), expected_revision=1)
    assert state["requires_new_packet"] and state["revision"] == 2


def test_checkpoint_allows_explicit_authorized_edit_evidence(task):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    (root / "source.txt").write_text("assigned implementation edit")
    current = proposal(manifest, evidence=[record(root, "source.txt")])
    state = s.checkpoint_update(root, "run.json", packet, current, expected_revision=0)
    assert state["revision"] == 1
    assert s.checkpoint_inspect(root, ["run.json"], packet)["candidates"][0]["drift"] == []
    (root / "source.txt").write_text("second assigned edit")
    current = proposal(current, evidence=[record(root, "source.txt")], state="completed")
    state = s.checkpoint_update(root, "run.json", packet, current, expected_revision=1)
    assert state["state"] == "completed"


def test_checkpoint_immutable_input_cannot_be_superseded_by_evidence(task):
    root, packet, manifest = task
    packet["allowed_edits"] = ["other.txt"]
    packet["prompt"] = h._make_prompt(packet)
    packet["packet_content_hash"] = h._packet_hash(packet)
    s.checkpoint_init(root, "run.json", packet, manifest)
    (root / "source.txt").write_text("changed contract input")
    with pytest.raises(h.HarnessError, match="quarantine required"):
        s.checkpoint_update(root, "run.json", packet,
            proposal(manifest, evidence=[record(root, "source.txt")]), expected_revision=0)


def test_checkpoint_retains_inherited_edit_binding_on_completion(task):
    root, packet, manifest = task
    s.checkpoint_init(root, "run.json", packet, manifest)
    (root / "source.txt").write_text("authorized edit")
    observed = record(root, "source.txt")
    s.checkpoint_update(root, "run.json", packet, proposal(manifest, evidence=[observed]), expected_revision=0)
    state = s.checkpoint_update(root, "run.json", packet, proposal(manifest, state="completed"), expected_revision=1)
    assert state["evidence"] == [observed]
    assert s.checkpoint_inspect(root, ["run.json"], packet)["candidates"][0]["drift"] == []


def test_review_overlap_cannot_hide_invalidated_dimension(review):
    root, snapshot, manifest = review
    source = {key: snapshot[key] for key in ("scope", "assumptions", "groups")}
    source["groups"].append({"id": "additional-independent", "files": ["a.py"],
                             "dependencies": [], "assumptions": ["independent"]})
    snapshot = s.review_snapshot(root, source)
    manifest["assumptions"]["oracle"] = "changed"
    result = s.review_check(root, snapshot, manifest)
    assert result["groups"][-1]["status"] == "retained"
    assert not result["coverage_complete"] and "a.py" in result["uncovered"]
