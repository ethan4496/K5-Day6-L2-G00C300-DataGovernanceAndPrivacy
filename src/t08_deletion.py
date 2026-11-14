"""T08 · Retention & Delete Request Simulator — core logic (pure functions, unit-testable).

Policy precedence (frozen before evaluation, see reports/metric_contract.md):
    1. legal_hold set on the artefact            -> retain_legal_hold   (LEGAL_HOLD)
    2. requester identity not verified           -> escalate            (IDENTITY_NOT_VERIFIED)
    3. operator role not in artefact ACL         -> escalate            (NO_PERMISSION_ACL)
    4. origin unknown (provenance gap)           -> escalate            (UNKNOWN_ORIGIN)
    5. artefact also holds other subjects' data  -> escalate            (MULTI_SUBJECT)
    6. derived from subject but untagged         -> escalate            (UNTAGGED_DERIVED)
    7. otherwise                                 -> delete              (SUBJECT_DATA)

Scope = artefacts tagged with the subject (seeds) + BFS over children (lineage).
A descendant tagged only with OTHER subjects is out of scope and never deleted: it is another
person's data that happens to be derived from a shared frame.
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path

import pandas as pd

ACTIONS = ("delete", "retain_legal_hold", "escalate")
PLAN_COLUMNS = ["request_id", "artefact_id", "action", "reason_code", "subject_id", "type", "found_via",
                "lineage_depth", "n_subjects", "legal_hold", "ancestor_hold", "uri", "deadline"]


# ---------------------------------------------------------------- 01 load
def load_inputs(input_dir: Path) -> tuple[dict[str, dict], list[dict]]:
    graph = json.loads((input_dir / "data_graph.json").read_text(encoding="utf-8"))
    requests = json.loads((input_dir / "requests.json").read_text(encoding="utf-8"))
    arts = {a["artefact_id"]: a for a in graph["artefacts"]}
    if len(arts) != len(graph["artefacts"]):
        raise ValueError("data_graph has duplicate artefact_id")
    dangling = sorted({p for a in arts.values() for p in a.get("parent_ids", []) if p not in arts})
    if dangling:
        raise ValueError(f"data_graph has parent_ids pointing to unknown artefacts: {dangling[:5]}")
    ids = [r["request_id"] for r in requests]
    if len(ids) != len(set(ids)):
        raise ValueError("requests.json has duplicate request_id")
    return arts, requests


def children_index(arts: dict[str, dict]) -> dict[str, list[str]]:
    ch: dict[str, list[str]] = defaultdict(list)
    for aid in sorted(arts):
        for p in arts[aid].get("parent_ids", []):
            ch[p].append(aid)
    return ch


def ancestors(aid: str, arts: dict[str, dict]) -> set[str]:
    seen: set[str] = set()
    stack = list(arts[aid].get("parent_ids", []))
    while stack:
        x = stack.pop()
        if x not in seen:
            seen.add(x)
            stack.extend(arts[x].get("parent_ids", []))
    return seen


# ---------------------------------------------------------------- 02 lineage + policy
def discover_scope(subject: str, arts: dict[str, dict], children: dict[str, list[str]]):
    """Return ({artefact_id: (found_via, depth)}, [out-of-scope descendant ids])."""
    seeds = sorted(a for a, v in arts.items() if subject in v.get("subject_ids", []))
    scope = {a: ("tag", 0) for a in seeds}
    out_of_scope: list[str] = []
    q = deque((a, 0) for a in seeds)
    visited = set(seeds)
    while q:
        aid, d = q.popleft()
        for c in children.get(aid, []):
            if c in visited:
                continue
            visited.add(c)
            subs = arts[c].get("subject_ids", [])
            if subject in subs:
                scope[c] = ("tag", d + 1)
            elif not subs:
                scope[c] = ("lineage", d + 1)         # derived, untagged -> needs a human
            else:
                out_of_scope.append(c)                # another person's data: never touch
                continue                               # and do not walk through it
            q.append((c, d + 1))
    return scope, sorted(out_of_scope)


def decide_action(art: dict, request: dict, found_via: str, operator_role: str) -> tuple[str, str]:
    if art.get("legal_hold"):
        return "retain_legal_hold", "LEGAL_HOLD"
    if not request.get("identity_verified", False):
        return "escalate", "IDENTITY_NOT_VERIFIED"
    if operator_role not in (art.get("acl") or []):
        return "escalate", "NO_PERMISSION_ACL"
    if not art.get("origin_known", True):
        return "escalate", "UNKNOWN_ORIGIN"
    if len(art.get("subject_ids", [])) > 1:
        return "escalate", "MULTI_SUBJECT"
    if found_via == "lineage":
        return "escalate", "UNTAGGED_DERIVED"
    return "delete", "SUBJECT_DATA"


def plan_deletions(arts: dict[str, dict], requests: list[dict], operator_role: str = "data-platform"):
    """Dry-run plan: one row per (request, in-scope artefact), deterministic order.
    Returns (plan DataFrame, list of out-of-scope descendants that were deliberately skipped)."""
    children = children_index(arts)
    rows, skipped = [], []
    for req in sorted(requests, key=lambda r: r["request_id"]):
        subject = req["subject_id"]
        scope, oos = discover_scope(subject, arts, children)
        skipped += [{"request_id": req["request_id"], "artefact_id": a, "type": arts[a]["type"],
                     "subject_ids": ";".join(arts[a]["subject_ids"])} for a in oos]
        for aid in sorted(scope):
            via, depth = scope[aid]
            a = arts[aid]
            action, code = decide_action(a, req, via, operator_role)
            held_anc = sorted(x for x in ancestors(aid, arts) if arts[x].get("legal_hold"))
            rows.append({
                "request_id": req["request_id"], "artefact_id": aid, "action": action, "reason_code": code,
                "subject_id": subject, "type": a["type"], "found_via": via, "lineage_depth": depth,
                "n_subjects": len(a.get("subject_ids", [])), "legal_hold": a.get("legal_hold") or "",
                "ancestor_hold": ";".join(arts[x]["legal_hold"] for x in held_anc), "uri": a["uri"],
                "deadline": req.get("deadline", ""),
            })
    return pd.DataFrame(rows, columns=PLAN_COLUMNS), skipped


# ---------------------------------------------------------------- 03 review priority
REVIEW_PRIORITY = {"LEGAL_HOLD": 1.0, "NO_PERMISSION_ACL": 0.95, "UNKNOWN_ORIGIN": 0.9, "IDENTITY_NOT_VERIFIED": 0.9,
                   "UNTAGGED_DERIVED": 0.85, "MULTI_SUBJECT": 0.8}
HELD_ANCESTOR_SCORE = 0.7


def review_score(row) -> float:
    """Higher = review first. Exceptions first; deletes downstream of a legal hold also need a human check."""
    s = REVIEW_PRIORITY.get(row["reason_code"], 0.0)
    if row["action"] == "delete" and row["ancestor_hold"]:
        s = HELD_ANCESTOR_SCORE
    if s > 0 and row["type"] == "training_export":
        s += 0.05                                      # broad blast radius
    return round(s, 4)


def review_reason(row) -> str:
    if row["action"] == "delete" and row["ancestor_hold"]:
        return f"HELD_ANCESTOR: delete under {row['ancestor_hold']}, confirm hold does not cover derived copies"
    return f"{row['reason_code']}: {row['type']} with {row['n_subjects']} subject(s)"


def item_key(request_id: str, artefact_id: str) -> str:
    return f"{request_id}|{artefact_id}"


# ---------------------------------------------------------------- 05 guardrails
def check_invariants(plan: pd.DataFrame, arts: dict[str, dict]) -> list[str]:
    """Hard rules that no reviewer override may break. Non-empty -> decision REWORK."""
    v = []
    if plan.duplicated(["request_id", "artefact_id"]).any():
        v.append("duplicate (request_id, artefact_id) rows")
    bad = sorted(set(plan.action) - set(ACTIONS))
    if bad:
        v.append(f"unknown actions {bad}")
    for r in plan[plan.action == "delete"].itertuples(index=False):
        a = arts[r.artefact_id]
        if a.get("legal_hold"):
            v.append(f"{r.request_id}/{r.artefact_id}: delete on legal hold {a['legal_hold']}")
        subs = a.get("subject_ids", [])
        if subs and r.subject_id not in subs:
            v.append(f"{r.request_id}/{r.artefact_id}: delete of another subject's artefact")
    return v


def override_allowed(art: dict, new_action: str) -> tuple[bool, str]:
    """Reviewer may change an action, but never turn a held / shared artefact into a delete."""
    if new_action not in ACTIONS:
        return False, f"unknown action {new_action!r}"
    if new_action == "delete" and art.get("legal_hold"):
        return False, f"legal hold {art['legal_hold']} cannot be overridden by a reviewer"
    if new_action == "delete" and len(art.get("subject_ids", [])) > 1:
        return False, "multi-subject artefact needs redaction, not deletion"
    return True, ""


# ---------------------------------------------------------------- 05 mock deletion (idempotent)
def mock_delete(plan: pd.DataFrame, state_path: Path, audit=None, actor: str = "pipeline") -> dict:
    """Tombstone `delete` rows in a mock store. Re-running is a no-op (idempotent)."""
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"tombstones": {}}
    tomb = state["tombstones"]
    n_new = n_already = 0
    for r in plan[plan.action == "delete"].itertuples(index=False):
        if r.artefact_id in tomb:
            n_already += 1
            if audit:
                audit.log(actor=actor, action="delete_mock:noop_already_deleted", item_id=r.artefact_id,
                          reason="idempotent re-run", request_id=r.request_id)
            continue
        tomb[r.artefact_id] = {"request_id": r.request_id, "uri": r.uri}
        n_new += 1
        if audit:
            audit.log(actor=actor, action="delete_mock", item_id=r.artefact_id, before=r.uri, after="tombstoned",
                      reason=r.reason_code, request_id=r.request_id)
    state_path.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    return {"deleted_new": n_new, "already_deleted": n_already, "tombstones_total": len(tomb)}
