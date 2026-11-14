"""PoC pipeline — T08 · Retention & Delete Request Simulator.

Lab contract = 5 automated steps, run by `python -m src.main ...`:
    01 load      requests + data graph, provenance check (manifest.csv)
    02 process   lineage BFS per request + policy precedence -> dry-run plan (one row per request x artefact)
    03 rank      review queue: exceptions + deletes under a held ancestor, highest risk first
    04 review    apply reviewer decisions (out/review_queue_reviewed.csv) with guardrails + audit log
    05 decide    GO / HOLD / REWORK; if GO and dry_run=false -> idempotent mock delete + evidence

Outputs (out/): actions.csv (graded), plan_dry_run.csv, review_queue.csv, contact_sheet.html,
request_summary.csv, out_of_scope.csv, decision.json, decision_log.csv, audit_log.jsonl,
mock_store_state.json + deletion_evidence.json (only when executed).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from . import t08_deletion as t8
from .common.contact_sheet import render_contact_sheet
from .common.context import RunContext
from .common.decision import Decision, write_decision
from .common.io import write_json
from .common.manifest import verify_manifest
from .common.review import load_review_decisions, make_review_queue, write_review_queue
from .metrics.tracks import get_track


def _require_t08(ctx: RunContext) -> None:
    if ctx.track != "t08":
        spec = get_track(ctx.track)
        raise NotImplementedError(f"this group's pipeline implements t08 only, not {spec.track} ({spec.title})")


# ---------------------------------------------------------------- 01 load
def load(ctx: RunContext) -> dict:
    manifest = ctx.input_dir / "manifest.csv"
    problems = verify_manifest(manifest) if manifest.exists() else [{"problem": "no_manifest"}]
    if problems:
        ctx.audit.log(actor="pipeline", action="provenance_problems", item_id=str(manifest), after=problems[:20])
        if ctx.config.get("strict_provenance", True):
            raise ValueError(f"provenance check failed ({len(problems)} problems), first: {problems[:3]}")
    arts, requests = t8.load_inputs(ctx.input_dir)
    ctx.stats.update(n_artefacts=len(arts), n_requests=len(requests))
    for r in requests:
        ctx.audit.log(actor="pipeline", action="request_received", item_id=r["request_id"],
                      after={"subject_id": r["subject_id"], "deadline": r.get("deadline"),
                             "identity_verified": r.get("identity_verified")})
    return {"arts": arts, "requests": requests}


# ---------------------------------------------------------------- 02 process
def process(ctx: RunContext, data: dict) -> pd.DataFrame:
    plan, skipped = t8.plan_deletions(data["arts"], data["requests"], ctx.param("operator_role", "data-platform"))
    pd.DataFrame(skipped, columns=["request_id", "artefact_id", "type", "subject_ids"]).to_csv(
        ctx.out("out_of_scope.csv"), index=False)
    for r in plan.itertuples(index=False):                   # one audit entry per planned action
        ctx.audit.log(actor="pipeline", action=f"plan:{r.action}", item_id=r.artefact_id, reason=r.reason_code,
                      request_id=r.request_id, found_via=r.found_via)
    plan["item_key"] = [t8.item_key(a, b) for a, b in zip(plan.request_id, plan.artefact_id)]
    plan["score"] = plan.apply(t8.review_score, axis=1)
    plan["review_reason"] = plan.apply(t8.review_reason, axis=1)
    plan.to_csv(ctx.out("plan_dry_run.csv"), index=False)
    ctx.stats["n_out_of_scope_skipped"] = len(skipped)
    ctx.stats["plan_counts"] = plan.action.value_counts().to_dict()
    return plan


# ---------------------------------------------------------------- 03 rank
def rank(ctx: RunContext, scored: pd.DataFrame) -> pd.DataFrame:
    need = scored[scored.score > 0]
    budget = int(ctx.param("review_budget", 60))
    q = make_review_queue(need, id_col="item_key", score_col="score", budget=budget, reason_col="review_reason",
                          action_col="action", slice_col="reason_code")
    write_review_queue(q, ctx.out("review_queue.csv"))
    render_contact_sheet(q, ctx.out("contact_sheet.html"), image_col=None,
                         title="T08 delete requests: exceptions to approve")
    ctx.stats.update(n_needs_review=len(need), n_queued=len(q), n_not_queued=max(0, len(need) - len(q)))
    return q


# ---------------------------------------------------------------- 04 review
def review(ctx: RunContext, scored: pd.DataFrame, queue: pd.DataFrame, arts: dict) -> pd.DataFrame:
    """approve = keep suggested action · edit/reject + override_value = change action · escalate · skip.
    Guardrail: a reviewer can never turn a legal-hold or multi-subject artefact into `delete`."""
    final = scored.copy()
    final["reviewer"] = ""
    final["review_decision"] = ""
    path = ctx.output_dir / "review_queue_reviewed.csv"
    if not path.exists():
        ctx.stats["n_reviewed"] = 0
        return final
    reviewed = load_review_decisions(path)
    pos = {k: i for i, k in enumerate(final.item_key)}
    n_refused = 0
    for r in reviewed.itertuples(index=False):
        if r.item_id not in pos:
            raise ValueError(f"reviewed item {r.item_id!r} is not in the current plan (stale review file?)")
        i = pos[r.item_id]
        aid = final.at[i, "artefact_id"]
        before = final.at[i, "action"]
        dec = r.reviewer_decision
        new = before
        if dec in {"edit", "reject"} and str(r.override_value).strip():
            new = str(r.override_value).strip().lower()
        elif dec == "escalate":
            new = "escalate"
        if new != before:
            ok, why = t8.override_allowed(arts[aid], new)
            if not ok:
                n_refused += 1
                ctx.audit.log(actor=r.reviewer, action="override_refused", item_id=aid, before=before, after=new,
                              reason=why, request_id=final.at[i, "request_id"], note=r.review_note)
                new = before
        final.at[i, "action"] = new
        final.at[i, "reviewer"] = r.reviewer
        final.at[i, "review_decision"] = dec
        ctx.audit.log(actor=r.reviewer, action=f"review:{dec}", item_id=aid, before=before, after=new,
                      reason=r.review_note, request_id=final.at[i, "request_id"])
    ctx.stats.update(n_reviewed=len(reviewed), n_override_refused=n_refused)
    return final


# ---------------------------------------------------------------- 05 decide
def decide(ctx: RunContext, final: pd.DataFrame, arts: dict) -> Decision:
    owner = ctx.config.get("owner", "TODO-owner")
    violations = t8.check_invariants(final, arts)
    needs = final[final.score > 0]
    pending = needs[needs.review_decision.isin(["", "skip"])]
    counts = final.action.value_counts().to_dict()
    metrics = {"n_rows": len(final), **{f"n_{k}": int(v) for k, v in counts.items()},
               "n_needs_review": len(needs), "n_pending_review": len(pending), "n_violations": len(violations)}
    ev = [str(ctx.out("actions.csv")), str(ctx.out("review_queue.csv")), str(ctx.out("audit_log.jsonl"))]
    if violations:
        return Decision("REWORK", "INVARIANT_VIOLATION", f"{len(violations)} guardrail violations, first: {violations[0]}",
                        owner, ctx.track, ev, metrics)
    if len(pending):
        return Decision("HOLD", "EXCEPTIONS_PENDING_REVIEW",
                        f"{len(pending)}/{len(needs)} exception rows still need a named reviewer; deletes not executed",
                        owner, ctx.track, ev, metrics)
    return Decision("GO", "DELETION_PLAN_APPROVED",
                    "All exceptions reviewed, no guardrail violation; deletes may be executed, escalations routed",
                    owner, ctx.track, ev, metrics)


def _request_summary(ctx: RunContext, final: pd.DataFrame, requests: list[dict]) -> pd.DataFrame:
    as_of = datetime.fromisoformat(ctx.param("as_of", "2026-11-14T00:00:00+07:00"))
    rows = []
    for r in sorted(requests, key=lambda x: x["request_id"]):
        sub = final[final.request_id == r["request_id"]]
        c = sub.action.value_counts()
        rows.append({"request_id": r["request_id"], "subject_id": r["subject_id"],
                     "identity_verified": r.get("identity_verified"), "deadline": r.get("deadline"),
                     "days_to_deadline": (datetime.fromisoformat(r["deadline"]) - as_of).days if r.get("deadline") else None,
                     "n_in_scope": len(sub), "n_delete": int(c.get("delete", 0)),
                     "n_retain_legal_hold": int(c.get("retain_legal_hold", 0)), "n_escalate": int(c.get("escalate", 0)),
                     "types_deleted": ";".join(sorted(sub[sub.action == "delete"].type.unique()))})
    return pd.DataFrame(rows)


def run(ctx: RunContext) -> None:
    _require_t08(ctx)
    spec = get_track(ctx.track)
    data = load(ctx)
    scored = process(ctx, data)
    ctx.stats["n_items"] = len(scored)
    queue = rank(ctx, scored)
    final = review(ctx, scored, queue, data["arts"])
    cols = spec.pred_columns + ["reason_code", "type", "found_via", "ancestor_hold", "reviewer", "review_decision"]
    final[cols].to_csv(ctx.out(spec.pred_file), index=False)
    _request_summary(ctx, final, data["requests"]).to_csv(ctx.out("request_summary.csv"), index=False)
    decision = decide(ctx, final, data["arts"])
    if decision.canonical == "GO" and not ctx.param("dry_run", True):
        res = t8.mock_delete(final, ctx.out("mock_store_state.json"), ctx.audit)
        ctx.stats["mock_delete"] = res
        evidence = {rid: sorted(g.artefact_id) for rid, g in final[final.action == "delete"].groupby("request_id")}
        write_json(ctx.out("deletion_evidence.json"), {"executed": True, "summary": res, "deleted_by_request": evidence})
        decision.evidence.append(str(ctx.out("deletion_evidence.json")))
    ctx.stats["decision"] = decision.canonical
    write_decision(decision, ctx.out("decision.json"), ctx.out("decision_log.csv"))
