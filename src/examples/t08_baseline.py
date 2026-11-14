"""T08 baseline (docs/tracks/T08.md §4): delete the raw file found by filename, nothing else.

No lineage (frames / crops / embeddings / backups / exports stay), no legal-hold or permission check.
It is the naive bar the PoC must beat on the same input / seed.

    python -m src.main --track t08 --pipeline src.examples.t08_baseline --output out/baseline --run-name baseline --offline
"""
from __future__ import annotations

import pandas as pd

from ..common.context import RunContext
from ..common.decision import Decision, write_decision
from ..t08_deletion import load_inputs


def run(ctx: RunContext) -> None:
    if ctx.track != "t08":
        raise NotImplementedError("t08_baseline only supports t08")
    arts, requests = load_inputs(ctx.input_dir)
    rows = []
    for r in sorted(requests, key=lambda x: x["request_id"]):
        prefix = f"drive_{r['subject_id']}_"
        for aid in sorted(arts):
            fn = arts[aid].get("filename", "")
            if fn.startswith(prefix) and fn.endswith(".mp4"):          # raw only, not .mp4.bak
                rows.append({"request_id": r["request_id"], "artefact_id": aid, "action": "delete"})
                ctx.audit.log(actor="baseline", action="plan:delete", item_id=aid, reason="FILENAME_MATCH",
                              request_id=r["request_id"])
    out = pd.DataFrame(rows, columns=["request_id", "artefact_id", "action"])
    out.to_csv(ctx.out("actions.csv"), index=False)
    ctx.stats["n_items"] = len(out)
    write_decision(Decision("HOLD", "BASELINE_ONLY", "Filename baseline: no lineage, no exception handling",
                            ctx.config.get("owner", "baseline"), ctx.track, [str(ctx.out("actions.csv"))]),
                   ctx.out("decision.json"), ctx.out("decision_log.csv"))
