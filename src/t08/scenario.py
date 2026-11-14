"""Self-made T08 problems with a known answer ("đề bài tự tạo").

`make_scenario(seed)` builds a small mock data graph and PLANTS the hard cases on purpose. The expected
action of every (request, artefact) pair is written down at the moment the artefact is created, from
which case was planted there — it is never computed by running the planner. That makes the answer key
independent of the code under test, so the official evaluator (`eval_t08`) can score the planner on it.

Planted cases (one subject each, roles shuffled by the seed):
    hold        raw video under legal hold                  -> whole family: retain_legal_hold
    vendor      backup whose acl is another party           -> that backup: escalate
    unknown     one embedding with origin_known = false     -> that embedding: escalate
    shared      frame showing owner + guest                 -> frame/annotation: escalate for both;
                                                               owner's raw + backup: escalate for the guest
    held+shared same, but the owner's raw is under hold     -> all of the above: retain_legal_hold
    unverified  requester whose identity is not verified    -> everything: escalate
    export      training export mixing several subjects     -> escalate for every requester inside
    bystander   subjects who never asked                    -> no row at all (touching them = false delete)
"""
from __future__ import annotations

import random

import pandas as pd

OWNER = "data-platform"
ROLES = ["hold", "vendor", "unknown", "shared_owner", "shared_guest", "held_owner", "held_guest",
         "plain", "unverified", "bystander_a", "bystander_b"]


def make_scenario(seed: int, max_frames: int = 4) -> tuple[list[dict], list[dict], pd.DataFrame]:
    """Returns (artefacts, requests, expected) where expected has request_id, artefact_id, expected_action."""
    rng = random.Random(seed)
    subjects = [f"p{idx:02d}" for idx in range(len(ROLES))]
    rng.shuffle(subjects)
    role = dict(zip(ROLES, subjects))
    requesters = [s for r, s in role.items() if not r.startswith("bystander")]
    rng.shuffle(requesters)
    request_of = {s: f"R-{k + 1:03d}" for k, s in enumerate(requesters)}
    requests = [{"request_id": rid, "subject_id": s, "channel": "app", "identity_verified": s != role["unverified"]}
                for s, rid in request_of.items()]

    artefacts: list[dict] = []
    expected: list[dict] = []
    first_crop: dict[str, str] = {}

    def add(kind: str, subject_ids: list[str], parents: list[str], **extra) -> str:
        aid = f"x{len(artefacts) + 1:04d}"
        artefacts.append({"artefact_id": aid, "type": kind, "subject_ids": subject_ids, "parent_ids": parents,
                          "uri": f"mock://{kind}/{aid}", "owner": OWNER, "acl": [OWNER], "legal_hold": None,
                          "origin_known": True} | extra)
        return aid

    def expect(subject: str, aid: str, action: str) -> None:
        if subject in request_of:                                   # bystanders get no answer rows
            expected.append({"request_id": request_of[subject], "artefact_id": aid, "expected_action": action})

    guest_of = {role["shared_owner"]: role["shared_guest"], role["held_owner"]: role["held_guest"]}
    for s in sorted(subjects):
        held = s in (role["hold"], role["held_owner"])
        # what a normal, single-subject artefact of this family should get
        own = "retain_legal_hold" if held else "escalate" if s == role["unverified"] else "delete"
        # what an artefact shared with / belonging to someone else should get
        other = "retain_legal_hold" if held else "escalate"

        raw = add("raw_video", [s], [], filename=f"drive_{s}.mp4", legal_hold=f"LH-{seed}-{s}" if held else None)
        expect(s, raw, own)
        vendor = s == role["vendor"]
        bak = add("backup", [s], [raw], filename=f"drive_{s}.mp4.bak", acl=["vendor-x"] if vendor else [OWNER])
        expect(s, bak, "escalate" if vendor else own)

        for k in range(rng.randint(2, max_frames)):
            frame = add("frame", [s], [raw])
            expect(s, frame, own)
            expect(s, add("annotation", [s], [frame]), own)
            crop = add("face_crop", [s], [frame])
            expect(s, crop, own)
            first_crop.setdefault(s, crop)
            lost = s == role["unknown"] and k == 0
            expect(s, add("embedding", [s], [crop], origin_known=not lost), "escalate" if lost else own)

        if s in guest_of:
            guest = guest_of[s]
            frame = add("frame", [s, guest], [raw])                 # one frame shows both people
            ann = add("annotation", [s, guest], [frame])
            for who in (s, guest):
                expect(who, frame, other)
                expect(who, ann, other)
            crop = add("face_crop", [s], [frame])                   # the crop is the owner's face only
            expect(s, crop, own)
            expect(s, add("embedding", [s], [crop]), own)
            expect(guest, raw, other)                               # the guest is visible in the owner's video
            expect(guest, bak, other)                               # ... and in its backup copy

    members = [role[r] for r in ("hold", "plain", "shared_owner", "unverified", "bystander_a")]
    export = add("training_export", sorted(members), [first_crop[m] for m in sorted(members)])
    for m in members:
        expect(m, export, "escalate")

    return artefacts, requests, pd.DataFrame(expected, columns=["request_id", "artefact_id", "expected_action"])
