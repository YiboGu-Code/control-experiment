"""Shared deterministic eligibility and work selection; no scheduling or model call."""

import hashlib
import json

from ..models import Role, RoleCallStarted, RoleCallFinished, StateCommitted
from ..runtime import InvalidSubmission


class WorkPolicy:
    def __init__(self, workspace):
        self.workspace = workspace

    @staticmethod
    def _art_reviewed(state, art):
        if not art:
            return False
        if art["design_ref"] == state.design.document_ref.model_dump(mode="json"):
            return True
        # A no_op is a valid explicit decision to retain assets after a design
        # revision. It must have seen that revision, rather than an earlier design.
        designer_requests = {e.payload.request.request_id for e in state.ledger.events
                             if isinstance(e.payload, RoleCallStarted) and e.payload.request.role == Role.DESIGNER}
        last_design_commit = max((e.payload.state_revision for e in state.ledger.events
            if isinstance(e.payload, StateCommitted) and e.payload.request_id in designer_requests), default=0)
        return any(isinstance(e.payload, RoleCallFinished) and e.payload.result.role == Role.ART_PRODUCER and
            e.payload.result.status == "no_op" and e.payload.result.state_revision >= last_design_commit
            for e in state.ledger.events)

    def _report(self, state, identity):
        refs = [r for r in state.artifacts if r.artifact_id == identity]
        if not refs:
            return None
        ref = max(refs, key=lambda r: r.version)
        raw = self.workspace.read_public(ref.path).read_bytes()
        if not ref.sha256 or hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise InvalidSubmission("Controller handoff checksum mismatch")
        return json.loads(raw)

    def plan(self, state, role):
        issues = tuple(i.issue_id for i in state.issues if i.status != "closed" and i.suggested_role == role)
        game = next((g for g in state.game_revisions if g.revision_id == state.current_game_revision_id), None)
        if role == Role.DESIGNER:
            return (state.design is None or bool(issues)), "Initialize design or address assigned issues", issues
        if state.design is None or not state.requirements:
            return False, "Waiting for a grounded Designer specification", ()
        art = self._report(state, "art-manifest")
        art_reviewed = self._art_reviewed(state, art)
        if role == Role.ART_PRODUCER:
            unresolved = [a for a in (art or {}).get("assets", ())
                          if (a.get("normalization") or {}).get("status") in ("unresolved", "rejected")]
            if unresolved:
                work = tuple(dict.fromkeys((*issues, *(r for a in unresolved for r in a["request"]["requirement_ids"]))))
                return True, "Resolve source-only layouts and replace quality-rejected assets with clean standalone frames", work
            needed = not art_reviewed or bool(issues)
            return needed, "Synchronize art with the design or address assigned issues", issues
        if not art_reviewed:
            return False, "Waiting for Art production or an explicit retention decision on the current design", ()
        developer = self._report(state, "developer-report")
        player = self._report(state, "player-report")
        if role == Role.DEVELOPER:
            art_ref = max((r for r in state.artifacts if r.artifact_id == "art-manifest"), key=lambda r: r.version)
            upstream_changed = not developer or developer["design_ref"] != state.design.document_ref.model_dump(mode="json") or developer["art_manifest_ref"] != art_ref.model_dump(mode="json")
            unimplemented = tuple(r.requirement_id for r in state.requirements if r.status in ("not_implemented", "verified_fail"))
            # Packaging happens inside Developer, so the final project's exact bytes
            # receive new build, Player and QA evidence instead of reusing old verdicts.
            packaged = {p["trace_ref"]["sha256"] for p in (developer or {}).get("packaged_traces", [])}
            needs_traces = bool(game and player and player["game_revision_id"] == game.revision_id and
                any(s["mode"] == "capture" and s["status"] == "passed" and s["trace_ref"] and
                    s["trace_ref"]["sha256"] not in packaged for s in player["sessions"]))
            return bool(not game or game.build_status != "passed" or upstream_changed or issues or unimplemented or needs_traces), \
                "Implement current inputs, repair assigned issues, or assemble captured traces", tuple(dict.fromkeys((*issues, *unimplemented)))
        if not game or not developer or developer["game_revision_id"] != game.revision_id:
            return False, "Waiting for a current Developer game and handoff", ()
        if role == Role.PLAYER:
            if game.build_status != "passed":
                return False, "Current game has not passed basic engine checks", ()
            return not player or player["game_revision_id"] != game.revision_id or bool(issues), \
                "Collect current-game play/replay evidence or investigate assigned issues", issues
        # A failed build may still be reviewed. QA diagnoses missing evidence;
        # Player is never sent an unlaunchable game merely to fill its slot.
        return state.readiness is None or state.readiness.game_revision_id != game.revision_id or bool(issues), \
            "Review changes and current evidence or address assigned review issues", issues

