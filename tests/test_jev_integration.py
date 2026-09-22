from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from integrations.jev.client import JevAuthenticationError, JevClient, STUB_MODEL, redact_sensitive
from integrations.jev.decision import JevDecisionLayer, OutboundPrivacyError, serialize_outbound
from integrations.jev.eval import classification_metrics


def state() -> dict[str, object]:
    return {"candidate_id": "C-1", "title": "Public candidate", "observation": "public fact", "provenance_count": 1}


def test_stub_is_explicit_and_neutral() -> None:
    result = JevClient(force_stub=True).ask(state(), {"kind": {"type": "choice", "criteria": {"carry": "", "none_of_these": ""}}}, item_id="C-1")
    assert result.backend == "stub"
    assert result.model_returned == STUB_MODEL
    assert result.backend_reason == "forced_stub"


def test_unknown_fields_and_secret_values_are_rejected() -> None:
    with pytest.raises(OutboundPrivacyError):
        serialize_outbound("candidate_triage", {**state(), "raw_blob": "x"})
    with pytest.raises(OutboundPrivacyError):
        serialize_outbound("candidate_triage", {**state(), "observation": "api_key=secret-value-123"})


def test_redaction_handles_nested_values() -> None:
    assert "Bearer secret" not in json.dumps(redact_sensitive({"x": "Bearer secret-token-value"}))


def test_decision_layer_adds_review_on_low_confidence() -> None:
    layer = JevDecisionLayer(JevClient(force_stub=True))
    result = layer.decide(decision_type="candidate_triage", item_id="C-1", state=state())
    assert result.review_required
    assert "low_confidence" in result.review_reasons
    assert result.policy_result == "annotate_or_expand_never_shrink"


def test_decision_layer_never_authorizes_sensitive_actions() -> None:
    assert not JevDecisionLayer.may_authorize("kill")
    assert not JevDecisionLayer.may_authorize("place_order")
    assert JevDecisionLayer.may_authorize("annotate")


def test_live_auth_rejection_stops() -> None:
    def opener(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.test", 401, "bad", {}, None)

    with pytest.raises(JevAuthenticationError):
        JevClient(api_key="test", opener=opener, sleep=lambda _: None).ask(state(), {}, item_id="C-1")


def test_eval_reports_macro_f1_and_confusion() -> None:
    report = classification_metrics(["a", "a", "b"], ["a", "b", "b"])
    assert report["count"] == 3
    assert 0 < report["macro_f1"] < 1
    assert report["confusion"]["a->b"] == 1


def test_contract_example_is_valid_json() -> None:
    contract = json.loads(Path("examples/jev-decision-contract.json").read_text())
    assert contract["activation"]["default"] == "disabled"
