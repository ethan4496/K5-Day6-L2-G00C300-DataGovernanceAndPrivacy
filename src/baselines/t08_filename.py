"""T08 baseline from docs/tracks/T08.md: delete ONE raw copy found by filename, ignore derived data and backups.

    python -m src.main --track t08 --pipeline src.baselines.t08_filename --output out/baseline --run-name baseline --offline --seed 42

It is intentionally naive — the bar the PoC must beat on the same input, same seed.
"""
from __future__ import annotations

import json

import pandas as pd

from ..common.context import RunContext
from ..common.decision import Decision, write_decision
from ..common.review import make_review_queue, write_review_queue
from ..metrics.tracks import get_track


def baseline_plan(artefacts: list[dict], requests: list[dict]) -> pd.DataFrame:
    """For each request: the first raw_video whose filename contains the subject id -> delete. Nothing else."""
    rows = []
    for req in requests:
        hits = sorted((a for a in artefacts if a.get("type") == "raw_video" and req["subject_id"] in str(a.get("filename", ""))),
                      key=lambda a: a["artefact_id"])
        if hits:
            rows.append({"request_id": req["request_id"], "artefact_id": hits[0]["artefact_id"], "action": "delete",
                         "reason": "filename match", "score": 0.0})
    return pd.DataFrame(rows, columns=["request_id", "artefact_id", "action", "reason", "score"])


def run(ctx: RunContext) -> None:
    spec = get_track(ctx.track)
    if spec.track != "t08":
        raise NotImplementedError(f"t08_filename baseline only supports t08 (not {ctx.track})")
    artefacts = json.loads((ctx.input_dir / "data_graph.json").read_text(encoding="utf-8"))["artefacts"]
    requests = json.loads((ctx.input_dir / "requests.json").read_text(encoding="utf-8"))
    plan = baseline_plan(artefacts, requests)
    ctx.stats["n_items"] = len(plan)
    for r in plan.itertuples(index=False):
        ctx.audit.log(actor="baseline", action="plan:delete", item_id=r.artefact_id, reason=r.reason, request_id=r.request_id)
    queue = make_review_queue(plan.assign(item_id=plan.request_id + "|" + plan.artefact_id), "item_id", "score",
                              budget=len(plan), reason_col="reason", action_col="action")
    write_review_queue(queue, ctx.out("review_queue.csv"))
    plan.to_csv(ctx.out(spec.pred_file), index=False)
    write_decision(Decision(status="HOLD", reason_code="BASELINE_ONLY", reason="Filename baseline: never execute deletions from this plan",
                            owner=ctx.config.get("owner", "baseline"), scope=ctx.track, evidence=[str(ctx.out(spec.pred_file))]),
                   ctx.out("decision.json"), ctx.out("decision_log.csv"))
