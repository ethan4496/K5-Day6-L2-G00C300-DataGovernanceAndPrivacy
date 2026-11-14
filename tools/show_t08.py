"""Print a readable summary of T08 runs (baseline vs PoC) — no ground truth needed.

    make t08                                     # run baseline + PoC, then this
    .venv/bin/python -m tools.show_t08 --baseline out/baseline --poc out/poc

All checks here are label-free (computed from the input graph), so they are self-checks,
NOT the official metric. Official Deletion Completeness needs the instructor's gt/.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from src.t08_deletion import load_inputs


def self_checks(acts: pd.DataFrame, arts: dict, requests: list[dict]) -> dict:
    """Label-free checks against the input graph."""
    subj = {r["request_id"]: r["subject_id"] for r in requests}
    tagged = {(rid, a) for rid, s in subj.items() for a, v in arts.items() if s in v["subject_ids"]}
    acted = set(zip(acts.request_id, acts.artefact_id))
    dele = acts[acts.action == "delete"]
    on_hold = sum(1 for a in dele.artefact_id if arts[a].get("legal_hold"))
    shared = sum(1 for a in dele.artefact_id if len(arts[a]["subject_ids"]) > 1)
    other = sum(1 for r, a in zip(dele.request_id, dele.artefact_id)
                if arts[a]["subject_ids"] and subj[r] not in arts[a]["subject_ids"])
    return {
        "rows in actions.csv": len(acts),
        "delete / escalate / retain": f"{len(dele)} / {(acts.action == 'escalate').sum()} / {(acts.action == 'retain_legal_hold').sum()}",
        "tagged artefacts covered (has an action)": f"{len(acted & tagged)}/{len(tagged)} = {len(acted & tagged) / max(1, len(tagged)):.1%}",
        "artefact types deleted": ", ".join(sorted(dele.artefact_id.map(lambda a: arts[a]["type"]).unique())) or "-",
        "UNSAFE: deletes on legal hold": on_hold,
        "UNSAFE: deletes on multi-subject": shared,
        "UNSAFE: deletes of another subject's data": other,
    }


def show_run(name: str, out: Path, arts: dict, requests: list[dict]) -> dict | None:
    f = out / "actions.csv"
    if not f.exists():
        print(f"\n[{name}] {f} not found — run it first")
        return None
    acts = pd.read_csv(f)
    dec = json.loads((out / "decision.json").read_text()) if (out / "decision.json").exists() else {}
    checks = self_checks(acts, arts, requests)
    print(f"\n=== {name.upper()}  ({out})")
    print(f"decision: {dec.get('canonical', '?')}  [{dec.get('reason_code', '')}]  {dec.get('reason', '')}")
    print(f"owner   : {dec.get('owner', '?')}")
    return checks


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default="fixtures/t08_deletion")
    ap.add_argument("--baseline", default="out/baseline")
    ap.add_argument("--poc", default="out/poc")
    a = ap.parse_args(argv)
    arts, requests = load_inputs(Path(a.input))
    print(f"T08 · {len(arts)} artefacts, {len(requests)} delete requests  (input={a.input})")

    res = {n: show_run(n, Path(p), arts, requests) for n, p in [("baseline", a.baseline), ("poc", a.poc)]}
    have = {k: v for k, v in res.items() if v}
    if have:
        print("\n=== SELF-CHECKS (label-free, not the official metric)")
        print(pd.DataFrame(have).to_string())

    poc = Path(a.poc)
    if (poc / "request_summary.csv").exists():
        print("\n=== POC · per request (SLA = days to deadline)")
        print(pd.read_csv(poc / "request_summary.csv").drop(columns=["deadline", "types_deleted"]).to_string(index=False))
    if (poc / "actions.csv").exists():
        acts = pd.read_csv(poc / "actions.csv")
        exc = acts[acts.action != "delete"]
        print(f"\n=== POC · exceptions ({len(exc)}) by reason")
        print(exc.groupby(["action", "reason_code"]).size().rename("n").reset_index().to_string(index=False))
    if (poc / "out_of_scope.csv").exists():
        oos = pd.read_csv(poc / "out_of_scope.csv")
        print(f"\n=== POC · other people's data under shared frames, deliberately NOT touched ({len(oos)})")
        print(oos.to_string(index=False) if len(oos) else "(none)")
    if (poc / "review_queue.csv").exists():
        q = pd.read_csv(poc / "review_queue.csv", dtype=str).fillna("")
        done = (q.reviewer_decision != "").sum()
        print(f"\n=== POC · review queue: {len(q)} rows, {done} with a decision in this file — top 5:")
        print(q.head(5)[["rank", "item_id", "suggested_action", "reason"]].to_string(index=False))
        print(f"\nTo review: open {poc / 'contact_sheet.html'} (or edit {poc / 'review_queue.csv'}),"
              f" save as {poc / 'review_queue_reviewed.csv'}, then `make t08` again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
