"""T08 group tests: policy precedence, lineage traps, guardrails, idempotency, end-to-end contract."""
import json
from pathlib import Path

import pandas as pd
import pytest

from src import main as runner
from src import t08_deletion as t8


def art(aid, typ, subs, parents=(), **kw):
    a = {"artefact_id": aid, "type": typ, "subject_ids": list(subs), "parent_ids": list(parents),
         "uri": f"mock://{aid}", "owner": "data-platform", "acl": ["data-platform"], "legal_hold": None,
         "origin_known": True}
    a.update(kw)
    return a


@pytest.fixture
def mini():
    """subj_A: raw -> frame -> crop -> embedding, plus backup.
    A shared frame (A+B) under B's raw, with a crop that belongs only to B (trap for A's request)."""
    arts = [
        art("raw_a", "raw_video", ["A"]),
        art("bak_a", "backup", ["A"], ["raw_a"]),
        art("fr_a", "frame", ["A"], ["raw_a"]),
        art("crop_a", "face_crop", ["A"], ["fr_a"]),
        art("emb_a", "embedding", ["A"], ["crop_a"]),
        art("emb_untagged", "embedding", [], ["crop_a"]),
        art("raw_b", "raw_video", ["B"], legal_hold="LH-1"),
        art("fr_ab", "frame", ["A", "B"], ["raw_b"]),
        art("crop_b", "face_crop", ["B"], ["fr_ab"]),
        art("vendor_a", "backup", ["A"], ["raw_a"], acl=["vendor-x"]),
        art("legacy_a", "embedding", ["A"], ["crop_a"], origin_known=False),
    ]
    return {a["artefact_id"]: a for a in arts}


def plan_for(arts, subject="A", verified=True):
    req = [{"request_id": "R1", "subject_id": subject, "deadline": "2026-12-01T00:00:00+07:00",
            "identity_verified": verified}]
    plan, skipped = t8.plan_deletions(arts, req)
    return plan.set_index("artefact_id"), skipped


def test_success_deletes_full_lineage_not_only_raw(mini):
    plan, _ = plan_for(mini)
    for aid in ["raw_a", "bak_a", "fr_a", "crop_a", "emb_a"]:
        assert plan.loc[aid, "action"] == "delete", aid


def test_exceptions_follow_policy_precedence(mini):
    plan, _ = plan_for(mini)
    assert plan.loc["fr_ab", "reason_code"] == "MULTI_SUBJECT" and plan.loc["fr_ab", "action"] == "escalate"
    assert plan.loc["vendor_a", "reason_code"] == "NO_PERMISSION_ACL"
    assert plan.loc["legacy_a", "reason_code"] == "UNKNOWN_ORIGIN"
    assert plan.loc["emb_untagged", "reason_code"] == "UNTAGGED_DERIVED"


def test_legal_hold_beats_everything():
    a = art("x", "raw_video", ["A", "B"], legal_hold="LH-9", acl=["vendor-x"], origin_known=False)
    assert t8.decide_action(a, {"identity_verified": False}, "tag", "data-platform") == ("retain_legal_hold", "LEGAL_HOLD")


def test_other_subjects_descendant_is_never_in_scope(mini):
    plan, skipped = plan_for(mini)
    assert "crop_b" not in plan.index                       # B's crop under the shared frame
    assert [s["artefact_id"] for s in skipped] == ["crop_b"]
    assert "raw_b" not in plan.index                         # B's raw is an ancestor, not A's data


def test_unverified_identity_escalates_everything(mini):
    plan, _ = plan_for(mini, verified=False)
    assert set(plan.action) == {"escalate"}


def test_subject_without_data_gives_empty_plan(mini):
    plan, _ = plan_for(mini, subject="NOBODY")
    assert len(plan) == 0


def test_guardrail_refuses_delete_on_hold_or_shared():
    assert t8.override_allowed(art("h", "raw_video", ["A"], legal_hold="LH-1"), "delete")[0] is False
    assert t8.override_allowed(art("s", "frame", ["A", "B"]), "delete")[0] is False
    assert t8.override_allowed(art("ok", "frame", ["A"]), "escalate")[0] is True
    assert t8.override_allowed(art("ok", "frame", ["A"]), "purge")[0] is False


def test_invariants_catch_delete_on_hold(mini):
    plan, _ = plan_for(mini)
    bad = plan.reset_index()
    bad.loc[bad.artefact_id == "fr_ab", "action"] = "delete"
    mini["fr_ab"]["legal_hold"] = "LH-2"
    assert any("legal hold" in v for v in t8.check_invariants(bad, mini))


def test_mock_delete_is_idempotent(mini, tmp_path):
    plan, _ = plan_for(mini)
    state = tmp_path / "state.json"
    first = t8.mock_delete(plan.reset_index(), state)
    second = t8.mock_delete(plan.reset_index(), state)
    assert first["deleted_new"] == 5 and second == {"deleted_new": 0, "already_deleted": 5, "tombstones_total": 5}


def test_dangling_parent_fails_loudly(tmp_path):
    (tmp_path / "data_graph.json").write_text(json.dumps({"artefacts": [art("x", "frame", ["A"], ["missing"])]}))
    (tmp_path / "requests.json").write_text("[]")
    with pytest.raises(ValueError, match="unknown artefacts"):
        t8.load_inputs(tmp_path)


# ---------------------------------------------------------------- end-to-end on the official fixture
def _run(out: Path, pipeline=None):
    args = ["--track", "t08", "--output", str(out), "--offline", "--seed", "42"]
    return runner.main(args + (["--pipeline", pipeline] if pipeline else []))


def test_e2e_fixture_contract_and_reproducible(tmp_path):
    assert _run(tmp_path / "a") == 0
    assert _run(tmp_path / "b") == 0
    a = (tmp_path / "a" / "actions.csv").read_text()
    assert a == (tmp_path / "b" / "actions.csv").read_text()
    acts = pd.read_csv(tmp_path / "a" / "actions.csv")
    assert not acts.duplicated(["request_id", "artefact_id"]).any()
    held = acts[acts.artefact_id.isin(["a0043", "a0099"])]
    assert set(held.action) == {"retain_legal_hold"}
    assert json.loads((tmp_path / "a" / "decision.json").read_text())["canonical"] == "HOLD"   # nobody reviewed yet
    logged = {json.loads(l)["item_id"] for l in (tmp_path / "a" / "audit_log.jsonl").read_text().splitlines()}
    assert set(acts.artefact_id) <= logged


def test_e2e_review_without_reviewer_name_fails(tmp_path):
    out = tmp_path / "r"
    assert _run(out) == 0
    q = pd.read_csv(out / "review_queue.csv", dtype=str).fillna("")
    q.loc[0, "reviewer_decision"] = "approve"                # no reviewer -> must fail clearly
    q.to_csv(out / "review_queue_reviewed.csv", index=False)
    assert _run(out) == 2


def test_baseline_misses_lineage_and_hits_legal_hold(tmp_path):
    assert _run(tmp_path / "base", "src.examples.t08_baseline") == 0
    acts = pd.read_csv(tmp_path / "base" / "actions.csv")
    assert len(acts) == 10 and {"a0043", "a0099"} <= set(acts.artefact_id)   # documents the failure mode
