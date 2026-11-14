"""T08 self-check: prove the planner's answers with a virtual system. Reads NO ground truth.

    python -m tools.t08_selfcheck                       # 25 self-made problems + the lab fixture
    python -m tools.t08_selfcheck --scenarios 100 --out out/t08_selfcheck

Three independent kinds of evidence:
  A  Self-made problems with a planted answer key (src/t08/scenario.py), scored by the OFFICIAL
     evaluator src/metrics/tracks.py::eval_t08 — for the PoC planner and for the filename baseline.
  B  The lab fixture: the plan is executed on a virtual store and the end state is checked (C1..C5).
     The baseline goes through the same checks and is expected to fail them.
  C  Mutation test: known-wrong plans are fed to the verifier; every one must be caught.
     (A verifier that never fails proves nothing.)

Exit code 0 only if A is perfect for the PoC, B passes for the PoC, and C catches every mutation.
All numbers in the report come from this script; do not edit them by hand.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from src.baselines.t08_filename import baseline_plan
from src.metrics.tracks import eval_t08
from src.t08.policy import plan_actions, resolve_params
from src.t08.scenario import make_scenario
from src.t08.virtual_system import all_passed, verify

FIXTURE = Path("fixtures/t08_deletion")


def _score(expected: pd.DataFrame, plan: pd.DataFrame, folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    expected.to_csv(folder / "expected_actions.csv", index=False)
    plan[["request_id", "artefact_id", "action"]].to_csv(folder / "actions.csv", index=False)
    out = eval_t08(folder / "expected_actions.csv", folder / "actions.csv", {})
    return {m.name: {"value": m.value, "num": m.numerator, "den": m.denominator} for m in out.metrics}


def part_a(n: int, out_dir: Path, params: dict) -> dict:
    rows = []
    for seed in range(n):
        artefacts, requests, expected = make_scenario(seed)
        poc = _score(expected, plan_actions(artefacts, requests, params), out_dir / "scenarios" / f"seed_{seed:03d}" / "poc")
        base = _score(expected, baseline_plan(artefacts, requests), out_dir / "scenarios" / f"seed_{seed:03d}" / "baseline")
        rows.append({"seed": seed, "artefacts": len(artefacts), "requests": len(requests), "answer_rows": len(expected),
                     "must_delete": poc["Deletion Completeness"]["den"],
                     "poc_completeness": poc["Deletion Completeness"]["value"],
                     "poc_false_delete": poc["False-delete count"]["value"],
                     "poc_exception_acc": poc["Exception handling accuracy"]["value"],
                     "base_completeness": base["Deletion Completeness"]["value"],
                     "base_false_delete": base["False-delete count"]["value"],
                     "base_exception_acc": base["Exception handling accuracy"]["value"]})
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "scenario_scores.csv", index=False)
    perfect = bool((df.poc_completeness == 1.0).all() and (df.poc_false_delete == 0).all() and (df.poc_exception_acc == 1.0).all())
    return {"n_scenarios": n, "poc_perfect_on_all": perfect,
            "answer_rows_total": int(df.answer_rows.sum()), "must_delete_total": int(df.must_delete.sum()),
            "poc": {"min_completeness": float(df.poc_completeness.min()), "total_false_delete": int(df.poc_false_delete.sum()),
                    "min_exception_acc": float(df.poc_exception_acc.min())},
            "baseline": {"mean_completeness": float(df.base_completeness.mean()), "total_false_delete": int(df.base_false_delete.sum()),
                         "mean_exception_acc": float(df.base_exception_acc.mean())}}


def _load_fixture() -> tuple[list[dict], list[dict]]:
    artefacts = json.loads((FIXTURE / "data_graph.json").read_text(encoding="utf-8"))["artefacts"]
    requests = json.loads((FIXTURE / "requests.json").read_text(encoding="utf-8"))
    return artefacts, requests


def part_b(params: dict) -> tuple[dict, pd.DataFrame, list[dict], list[dict]]:
    artefacts, requests = _load_fixture()
    plan = plan_actions(artefacts, requests, params)
    twice = plan_actions(artefacts, requests, params)
    poc_checks = verify(artefacts, requests, plan, params)
    base_checks = verify(artefacts, requests, baseline_plan(artefacts, requests), params)
    res = {"n_artefacts": len(artefacts), "n_requests": len(requests), "plan_rows": len(plan),
           "plan_actions": {a: int(n) for a, n in plan.action.value_counts().items()},
           "plan_reasons": {a: int(n) for a, n in plan.reason_code.value_counts().items()},
           "planner_deterministic": bool(plan.equals(twice)),
           "poc_all_passed": all_passed(poc_checks), "poc_checks": [c.to_dict() for c in poc_checks],
           "baseline_all_passed": all_passed(base_checks), "baseline_checks": [c.to_dict() for c in base_checks]}
    return res, plan, artefacts, requests


def part_c(plan: pd.DataFrame, artefacts: list[dict], requests: list[dict], params: dict) -> dict:
    """Break the correct plan in one specific way at a time; the named check must fail."""
    requesting = {r["subject_id"] for r in requests}

    def first(mask) -> int:
        return plan.index[mask][0]

    def flip(idx: int, action: str) -> pd.DataFrame:
        out = plan.copy()
        out.loc[idx, "action"] = action
        return out

    rid0 = requests[0]["request_id"]
    in_plan = set(plan.loc[plan.request_id == rid0, "artefact_id"])
    bystander = next(a["artefact_id"] for a in artefacts
                     if not set(a["subject_ids"]) & requesting and a["artefact_id"] not in in_plan)
    stray = pd.DataFrame([{"request_id": rid0, "artefact_id": bystander, "action": "delete"}])
    container = first(plan.reason_code == "IMPLICIT_CONTAINER")
    emb = first((plan.action == "delete") & (plan.type == "embedding"))
    mutations = [
        ("delete an artefact under legal hold", flip(first(plan.reason_code == "LEGAL_HOLD"), "delete"), "C2 no false delete"),
        ("delete an artefact derived from held data", flip(first(plan.reason_code == "LEGAL_HOLD_INHERITED"), "delete"), "C2 no false delete"),
        ("delete a frame shared with another person", flip(first(plan.reason_code == "MULTI_SUBJECT"), "delete"), "C2 no false delete"),
        ("delete a backup we have no permission on", flip(first(plan.reason_code == "NO_PERMISSION"), "delete"), "C2 no false delete"),
        ("delete an embedding of unknown origin", flip(first(plan.reason_code == "UNKNOWN_ORIGIN"), "delete"), "C2 no false delete"),
        ("delete data of a person who never asked", pd.concat([plan, stray], ignore_index=True), "C2 no false delete"),
        ("delete someone else's video because the requester appears in it", flip(container, "delete"), "C2 no false delete"),
        ("hide a delete behind a duplicate row", pd.concat([plan, plan.loc[[container]].assign(action="delete")], ignore_index=True), "C2 no false delete"),
        ("forget to delete one embedding", plan.drop(index=emb), "C3 no residual data"),
        ("keep an embedding by calling it an exception", flip(emb, "escalate"), "C3 no residual data"),
        ("drop the row of a legal-hold survivor", plan.drop(index=first(plan.reason_code == "LEGAL_HOLD")), "C4 exceptions documented"),
        ("label a legal hold as escalate", flip(first(plan.reason_code == "LEGAL_HOLD"), "escalate"), "C4 exceptions documented"),
        ("repeat a row", pd.concat([plan, plan.head(1)], ignore_index=True), "C1 well-formed plan"),
        ("use an action that does not exist", flip(plan.index[0], "purge"), "C1 well-formed plan"),
    ]
    results = []
    for name, mutated, must_fail in mutations:
        failed = [c.name for c in verify(artefacts, requests, mutated, params) if not c.passed]
        results.append({"mutation": name, "expected_check": must_fail, "failed_checks": failed, "caught": must_fail in failed})
    return {"n_mutations": len(results), "n_caught": sum(r["caught"] for r in results), "results": results}


def render(report: dict) -> str:
    a, b, c = report["A_planted_scenarios"], report["B_fixture"], report["C_mutations"]
    lines = ["# T08 self-check report (generated by tools/t08_selfcheck.py — do not edit by hand)", "",
             f"Overall: **{'PASS' if report['overall_pass'] else 'FAIL'}** · policy params: `{json.dumps(report['params'])}`", "",
             "## A. Self-made problems with a planted answer key, scored by the official `eval_t08`", "",
             f"{a['n_scenarios']} problems, {a['answer_rows_total']} answer rows, {a['must_delete_total']} must-delete rows.", "",
             "| Run | Deletion Completeness | False-delete count (total) | Exception handling accuracy |", "|---|---|---|---|",
             f"| PoC planner | min {a['poc']['min_completeness']:.4f} | {a['poc']['total_false_delete']} | min {a['poc']['min_exception_acc']:.4f} |",
             f"| Filename baseline | mean {a['baseline']['mean_completeness']:.4f} | {a['baseline']['total_false_delete']} | mean {a['baseline']['mean_exception_acc']:.4f} |", "",
             "## B. Lab fixture executed on the virtual store", "",
             f"{b['n_artefacts']} artefacts, {b['n_requests']} requests, {b['plan_rows']} plan rows: `{json.dumps(b['plan_actions'])}`.",
             f"Reasons: `{json.dumps(b['plan_reasons'])}`. Planner deterministic across two runs: {b['planner_deterministic']}.", "",
             "| Check | PoC | Baseline | Baseline violations |", "|---|---|---|---|"]
    base = {x["name"]: x for x in b["baseline_checks"]}
    for chk in b["poc_checks"]:
        if chk["name"] == "execution receipt":
            continue
        bc = base.get(chk["name"], {})
        lines.append(f"| {chk['name']} | {'PASS' if chk['passed'] else 'FAIL'} | {'PASS' if bc.get('passed') else 'FAIL'} | {bc.get('n_violations', '')} |")
    lines += ["", "## C. Mutation test — every known-wrong plan must be caught", "",
              f"{c['n_caught']} / {c['n_mutations']} caught.", "", "| Wrong plan | Check that must fail | Caught |", "|---|---|---|"]
    lines += [f"| {r['mutation']} | {r['expected_check']} | {'yes' if r['caught'] else 'NO'} |" for r in c["results"]]
    lines += ["", "## What this does and does not prove", "",
              "- It proves the planner implements the group's stated policy (docs/T08_DE_BAI_TU_TAO.md) without traversal bugs.",
              "- It does NOT prove the policy matches the instructor's hidden ground truth; that score needs `src.evaluate` with the overlay.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T08 self-check on a virtual system (no ground truth needed)")
    ap.add_argument("--scenarios", type=int, default=25)
    ap.add_argument("--out", default="out/t08_selfcheck")
    ap.add_argument("--config", default="configs/lab.yaml")
    a = ap.parse_args(argv)
    params = {}
    if Path(a.config).exists():
        import yaml
        params = (yaml.safe_load(Path(a.config).read_text(encoding="utf-8")) or {}).get("params") or {}
    params = resolve_params(params)
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    res_a = part_a(a.scenarios, out_dir, params)
    res_b, plan, artefacts, requests = part_b(params)
    res_c = part_c(plan, artefacts, requests, params)
    ok = res_a["poc_perfect_on_all"] and res_b["poc_all_passed"] and res_b["planner_deterministic"] and res_c["n_caught"] == res_c["n_mutations"]
    report = {"overall_pass": bool(ok), "params": params, "A_planted_scenarios": res_a, "B_fixture": res_b, "C_mutations": res_c}
    (out_dir / "selfcheck.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    (out_dir / "selfcheck.md").write_text(render(report), encoding="utf-8")
    print(render(report))
    print(f"[t08_selfcheck] {'PASS' if ok else 'FAIL'} -> {out_dir / 'selfcheck.md'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
