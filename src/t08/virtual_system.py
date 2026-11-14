"""Virtual storage system + verifier for T08.

`VirtualStore` is an in-memory copy of the mock artefact stores. Executing a plan on it deletes nothing real.
`verify()` then inspects the END STATE of the store and checks the properties a correct deletion must have.

The verifier does not reuse the planner: it re-derives every fact from the raw graph with its own code
(recursive walk up the parents instead of the planner's top-down pass), so a bug in the planner's
traversal shows up here as a violation instead of being silently repeated.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

import pandas as pd

ALLOWED_ACTIONS = {"delete", "retain_legal_hold", "escalate"}


class VirtualStore:
    def __init__(self, artefacts: list[dict]):
        self.live: dict[str, dict] = {a["artefact_id"]: a for a in artefacts}
        self.tombstones: dict[str, dict] = {}

    def execute(self, actions: pd.DataFrame) -> dict:
        """Apply every `delete` row. Deleting something already deleted is a no-op (idempotent)."""
        newly, already, unknown = 0, 0, 0
        for r in actions.itertuples(index=False):
            if r.action != "delete":
                continue
            aid = str(r.artefact_id)
            if aid in self.live:
                art = self.live.pop(aid)
                self.tombstones[aid] = {"request_id": str(r.request_id), "uri": art.get("uri", ""), "type": art.get("type", "")}
                newly += 1
            elif aid in self.tombstones:
                already += 1
            else:
                unknown += 1
        return {"newly_deleted": newly, "already_deleted": already, "unknown_artefact": unknown,
                "live": len(self.live), "tombstones": len(self.tombstones)}

    def state_hash(self) -> str:
        payload = json.dumps({"live": sorted(self.live), "tombstones": sorted(self.tombstones)})
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["n_violations"] = len(self.violations)
        d["violations"] = self.violations[:20]
        return d


def _hold_of(aid: str, by_id: dict, propagation: str, memo: dict) -> str | None:
    if aid in memo:
        return memo[aid]
    art = by_id[aid]
    hold = art.get("legal_hold") or None
    parents = art.get("parent_ids") or []
    if not hold and propagation == "derived" and parents:
        parent_holds = [_hold_of(p, by_id, propagation, memo) for p in parents]
        if all(parent_holds):
            hold = parent_holds[0]
    memo[aid] = hold
    return hold


def _exceptions(art: dict, by_id: dict, propagation: str, memo: dict) -> list[str]:
    """Why this artefact may lawfully survive a delete request (empty list = it must be deleted)."""
    why = []
    if _hold_of(art["artefact_id"], by_id, propagation, memo):
        why.append("legal_hold")
    if art.get("owner") not in (art.get("acl") or []):
        why.append("no_permission")
    if not art.get("origin_known", False):
        why.append("unknown_origin")
    if len(set(art.get("subject_ids") or [])) > 1:
        why.append("shared")
    return why


def verify(artefacts: list[dict], requests: list[dict], actions: pd.DataFrame, params: dict | None = None,
           audit_item_ids: set[str] | None = None) -> list[Check]:
    propagation = (params or {}).get("legal_hold_propagation", "derived")
    by_id = {a["artefact_id"]: a for a in artefacts}
    req_by_id = {r["request_id"]: r for r in requests}
    memo: dict = {}
    checks: list[Check] = []

    # C1 — the plan itself is well formed
    v = []
    missing = [c for c in ("request_id", "artefact_id", "action") if c not in actions.columns]
    if missing:
        return [Check("C1 well-formed plan", False, f"missing columns {missing}", [f"missing {missing}"])]
    acts = actions.assign(request_id=actions.request_id.astype(str), artefact_id=actions.artefact_id.astype(str),
                          action=actions.action.astype(str).str.strip().str.lower())
    for r in acts[acts.duplicated(["request_id", "artefact_id"], keep=False)].itertuples(index=False):
        v.append(f"duplicate row {r.request_id}|{r.artefact_id}")
    for r in acts.itertuples(index=False):
        if r.action not in ALLOWED_ACTIONS:
            v.append(f"{r.request_id}|{r.artefact_id}: unknown action {r.action!r}")
        if r.artefact_id not in by_id:
            v.append(f"{r.request_id}|{r.artefact_id}: unknown artefact")
        if r.request_id not in req_by_id:
            v.append(f"{r.request_id}|{r.artefact_id}: unknown request")
    checks.append(Check("C1 well-formed plan", not v, "unique (request, artefact) pairs, known ids, allowed actions", v))
    # Conflicting duplicate rows are judged by their most dangerous reading: if any copy says delete, it is a delete.
    acts = acts[acts.artefact_id.isin(by_id) & acts.request_id.isin(req_by_id)]
    acts = (acts.assign(_is_delete=(acts.action == "delete")).sort_values("_is_delete", ascending=False, kind="mergesort")
            .drop_duplicates(["request_id", "artefact_id"]).drop(columns="_is_delete").sort_index())

    # C2 — nothing was deleted that must not be deleted
    v = []
    for r in acts[acts.action == "delete"].itertuples(index=False):
        art, req = by_id[r.artefact_id], req_by_id[r.request_id]
        if req["subject_id"] not in (art.get("subject_ids") or []):
            v.append(f"{r.request_id}|{r.artefact_id}: deleted but not tagged with requester {req['subject_id']}")
        if not req.get("identity_verified", False):
            v.append(f"{r.request_id}|{r.artefact_id}: deleted for an unverified requester")
        why = _exceptions(art, by_id, propagation, memo)
        if why:
            v.append(f"{r.request_id}|{r.artefact_id}: deleted although exception {why} applies")
    checks.append(Check("C2 no false delete", not v, "every deleted artefact is single-subject, requester-owned, not held, permitted, origin known", v))

    # execute the plan on the virtual store, then look at what is left
    store = VirtualStore(artefacts)
    receipt = store.execute(acts)
    row_action = {(r.request_id, r.artefact_id): r.action for r in acts.itertuples(index=False)}

    # C3 — no identifiable data of a verified requester survives without a lawful reason
    # C4 — every survivor that does have a reason is documented with the right action
    residual, undocumented = [], []
    for req in requests:
        s, rid = req["subject_id"], req["request_id"]
        for aid, art in sorted(store.live.items()):
            if s not in (art.get("subject_ids") or []):
                continue
            why = _exceptions(art, by_id, propagation, memo)
            if not req.get("identity_verified", False):
                why = why + ["identity_not_verified"]
            if not why:
                residual.append(f"{rid}|{aid}: {art.get('type')} of {s} still live with no exception")
                continue
            expected = "retain_legal_hold" if "legal_hold" in why else "escalate"
            got = row_action.get((rid, aid))
            if got != expected:
                undocumented.append(f"{rid}|{aid}: survivor needs action {expected} ({why}), plan says {got}")
    checks.append(Check("C3 no residual data", not residual, "after execution, no requester-tagged artefact is left without an exception", residual))
    checks.append(Check("C4 exceptions documented", not undocumented, "every lawful survivor has a row with the right non-delete action", undocumented))

    # C5 — idempotency: executing the same plan again changes nothing
    h1 = store.state_hash()
    again = store.execute(acts)
    same = again["newly_deleted"] == 0 and store.state_hash() == h1
    checks.append(Check("C5 idempotent", same, f"second execution deleted {again['newly_deleted']} artefacts (must be 0), state hash unchanged={store.state_hash() == h1}",
                        [] if same else ["second execution changed the store"]))

    # C6 — audit trail (only when the caller passes the logged item ids)
    if audit_item_ids is not None:
        v = sorted({f"{r.request_id}|{r.artefact_id}: no audit entry" for r in acts.itertuples(index=False)
                    if r.artefact_id not in audit_item_ids})
        checks.append(Check("C6 audit complete", not v, "every action has an audit log entry", v))

    checks.append(Check("execution receipt", True, json.dumps(receipt), []))
    return checks


def all_passed(checks: list[Check]) -> bool:
    return all(c.passed for c in checks)
