"""T08 deletion planner: lineage traversal + exception precedence.

Pure functions, no I/O, no randomness: the same graph + requests + params always give the same plan.

Scope of a delete request for subject S
    tagged    : every artefact whose `subject_ids` contains S
    implicit  : artefacts NOT tagged S that still show S, because a tagged artefact was derived from
                them (single-parent ancestors), plus full copies (`copy_types`, default backup) of those

Precedence (first rule that matches wins)
    1 legal hold            -> retain_legal_hold   (own hold, or wholly derived from held artefacts)
    2 identity not verified -> escalate
    3 no permission         -> escalate            (artefact `owner` is not in its `acl`)
    4 unknown origin        -> escalate
    5 implicit container    -> escalate            (belongs to someone else; needs redaction, not deletion)
    6 multi-subject         -> escalate
    7 otherwise             -> delete

These rules are the group's ASSUMPTIONS (see docs/T08_DE_BAI_TU_TAO.md); they are not legal advice.
"""
from __future__ import annotations

from collections import deque

import pandas as pd

ACTIONS = ("delete", "retain_legal_hold", "escalate")
SCORE = {"escalate": 1.0, "retain_legal_hold": 0.5, "delete": 0.0}   # higher = review first

DEFAULT_PARAMS = {
    # "derived": an artefact is held if it has its own hold, or ALL of its parents are held
    # "self"   : only the artefact that carries the flag is held
    "legal_hold_propagation": "derived",
    "include_implicit_containers": True,
    "copy_types": ["backup"],
}


def resolve_params(params: dict | None) -> dict:
    out = dict(DEFAULT_PARAMS)
    out.update({k: v for k, v in (params or {}).items() if k in DEFAULT_PARAMS and v is not None})
    if out["legal_hold_propagation"] not in ("derived", "self"):
        raise ValueError(f"legal_hold_propagation must be 'derived' or 'self', got {out['legal_hold_propagation']!r}")
    return out


class Graph:
    """Artefact lineage graph (parent -> child). Rejects duplicate ids, dangling parents and cycles."""

    def __init__(self, artefacts: list[dict]):
        self.by_id: dict[str, dict] = {}
        for a in artefacts:
            aid = a["artefact_id"]
            if aid in self.by_id:
                raise ValueError(f"duplicate artefact_id {aid}")
            self.by_id[aid] = a
        self.children: dict[str, list[str]] = {aid: [] for aid in self.by_id}
        for aid, a in self.by_id.items():
            for p in a.get("parent_ids") or []:
                if p not in self.by_id:
                    raise ValueError(f"artefact {aid} has dangling parent {p}")
                self.children[p].append(aid)
        self.order = self._topological_order()

    def _topological_order(self) -> list[str]:
        indeg = {aid: len(a.get("parent_ids") or []) for aid, a in self.by_id.items()}
        ready = deque(sorted(aid for aid, d in indeg.items() if d == 0))
        order = []
        while ready:
            cur = ready.popleft()
            order.append(cur)
            for c in sorted(self.children[cur]):
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        if len(order) != len(self.by_id):
            raise ValueError("lineage graph has a cycle")
        return order

    def source_ancestors(self, start: set[str]) -> set[str]:
        """Ancestors reached through single-parent links only.

        If X was derived from exactly one parent, whatever X shows was already in that parent.
        An artefact with several parents is an aggregation (e.g. a training export): the subject
        came from SOME parent, not from all of them, so nothing is inferred through it.
        """
        seen: set[str] = set()
        queue = deque(start)
        while queue:
            parents = self.by_id[queue.popleft()].get("parent_ids") or []
            if len(parents) != 1:
                continue
            if parents[0] not in seen:
                seen.add(parents[0])
                queue.append(parents[0])
        return seen

    def holds(self, propagation: str) -> dict[str, tuple[str, str]]:
        """artefact_id -> (hold id, artefact the hold comes from)."""
        held: dict[str, tuple[str, str]] = {}
        for aid in self.order:                       # parents are always visited before children
            a = self.by_id[aid]
            if a.get("legal_hold"):
                held[aid] = (str(a["legal_hold"]), aid)
                continue
            parents = a.get("parent_ids") or []
            if propagation == "derived" and parents and all(p in held for p in parents):
                held[aid] = held[parents[0]]
        return held


def _classify(art: dict, verified: bool, implicit: bool, hold: tuple[str, str] | None) -> tuple[str, str, str]:
    aid = art["artefact_id"]
    if hold:
        hold_id, src = hold
        if src == aid:
            return "retain_legal_hold", "LEGAL_HOLD", f"artefact is under legal hold {hold_id}; delete when the hold is released"
        return "retain_legal_hold", "LEGAL_HOLD_INHERITED", f"wholly derived from {src}, which is under legal hold {hold_id}"
    if not verified:
        return "escalate", "IDENTITY_NOT_VERIFIED", "requester identity is not verified; nothing is deleted until it is"
    if art.get("owner") not in (art.get("acl") or []):
        return "escalate", "NO_PERMISSION", f"owner {art.get('owner')} is not in acl {art.get('acl')}; the acl holder must delete or grant access"
    if not art.get("origin_known", False):
        return "escalate", "UNKNOWN_ORIGIN", "origin unknown: cannot confirm what this artefact was derived from"
    if implicit:
        return "escalate", "IMPLICIT_CONTAINER", "not tagged with the subject, but a tagged artefact was derived from it; needs redaction"
    others = len(set(art.get("subject_ids") or [])) - 1
    if others > 0:
        return "escalate", "MULTI_SUBJECT", f"shared with {others} other subject(s); deleting it would remove their data too"
    return "delete", "DELETE_OK", "single-subject artefact of the requester, no exception applies"


def scope_of(graph: Graph, subject: str, params: dict) -> tuple[set[str], set[str]]:
    """(tagged, implicit) artefact ids for one subject."""
    tagged = {aid for aid, a in graph.by_id.items() if subject in (a.get("subject_ids") or [])}
    implicit: set[str] = set()
    if params["include_implicit_containers"] and tagged:
        containers = graph.source_ancestors(tagged) - tagged
        copies = {c for p in containers for c in graph.children[p]
                  if graph.by_id[c].get("type") in params["copy_types"]}
        implicit = (containers | copies) - tagged
    return tagged, implicit


PLAN_COLUMNS = ["request_id", "artefact_id", "action", "subject_id", "type", "reason_code", "reason", "in_scope_as",
                "score", "item_id", "uri"]


def plan_actions(artefacts: list[dict], requests: list[dict], params: dict | None = None) -> pd.DataFrame:
    """One row per (request, artefact in scope). Deterministic: sorted by request order, then artefact_id."""
    p = resolve_params(params)
    graph = Graph(artefacts)
    held = graph.holds(p["legal_hold_propagation"])
    seen_requests: set[str] = set()
    rows = []
    for req in requests:
        rid = req["request_id"]
        if rid in seen_requests:
            raise ValueError(f"duplicate request_id {rid}")
        seen_requests.add(rid)
        subject = req["subject_id"]
        verified = bool(req.get("identity_verified", False))
        tagged, implicit = scope_of(graph, subject, p)
        for aid in sorted(tagged | implicit):
            art = graph.by_id[aid]
            action, code, reason = _classify(art, verified, aid in implicit, held.get(aid))
            rows.append({
                "request_id": rid, "artefact_id": aid, "action": action, "subject_id": subject,
                "type": art.get("type", ""), "reason_code": code, "reason": reason,
                "in_scope_as": "implicit" if aid in implicit else "tagged",
                "score": SCORE[action], "item_id": f"{rid}|{aid}", "uri": art.get("uri", ""),
            })
    return pd.DataFrame(rows, columns=PLAN_COLUMNS)
