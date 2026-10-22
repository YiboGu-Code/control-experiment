from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from adaptive_game_generation.models import (
    ArtifactRef, DesignSpec, EvidenceRecord, GameRevision, ProposedUpdates,
    ReadinessReview, Requirement, ResourceLimits, ResourceUsage,
    RoleCallStarted, RoleResult, Verification,
)
from adaptive_game_generation.models.base import utc_now
from adaptive_game_generation.runtime import (
    BudgetExceeded, DecisionProposal, DockerSandbox, Executor, InvalidSubmission,
    PermissionDenied, PublicResource, RunBusy, RuntimeConfig, Services, TextReply, Workspace,
)
from adaptive_game_generation.runtime.errors import SandboxCleanupFailed
from adaptive_game_generation.runtime.workspace import inspect_tree


class FakeText:
    def __init__(self, *, failure=False, overrun=False):
        self.calls = 0
        self.failure, self.overrun = failure, overrun

    def input_token_upper_bound(self, prompt):
        return 10

    async def generate(self, prompt, *, max_output_tokens, timeout):
        self.calls += 1
        if self.failure:
            raise OSError("credential-SHOULD-NOT-LEAK")
        return TextReply("response", 30 if self.overrun else 6, 2, 2)


def make_run(tmp_path, *, controller="fixed_loop", budget=None, config=None, services=Services()):
    source = tmp_path / "benchmark" / "tasks" / "demo"
    source.mkdir(parents=True)
    (source / "instruction.md").write_text("Make a game. Player can jump.", encoding="utf-8")
    (source / "tests").mkdir()
    (source / "tests" / "rubric.json").write_text("HIDDEN-RUBRIC", encoding="utf-8")
    (source / "solution").mkdir()
    (source / "solution" / "solve.py").write_text("HIDDEN-SOLUTION", encoding="utf-8")
    (source / ".env").write_text("SECRET=credential", encoding="utf-8")
    (source / "workspace").mkdir()
    (source / "workspace" / "public.txt").write_text("public starter", encoding="utf-8")
    workspace, task = Workspace.prepare(tmp_path / "generation", source, task_id="demo")
    return Executor.create(
        workspace, task, run_id="run", controller=controller,
        budget=budget or ResourceLimits(max_model_calls=20, max_input_tokens=10000, max_output_tokens=10000),
        config=config, services=services,
    )


def result(context, **changes):
    return RoleResult(
        request_id=context.request.request_id, role=context.request.role,
        state_revision=context.request.state_revision, status="completed", summary="Done", **changes,
    )


async def design(context):
    path = await context.tools.write_text("design.md", "Player jumps with space.")
    ref = ArtifactRef(artifact_id="design", kind="document", path=path, created_by="designer")
    return result(context, artifact_refs=(ref,), proposed_updates=ProposedUpdates(
        requirements=(Requirement(
            requirement_id="R1", description="Jump", source_excerpt="Player can jump.",
            acceptance_criteria=("Player jumps when space is pressed",),
        ),), design=DesignSpec(version=1, document_ref=ref, change_summary="Initial design"),
    ))


def invoke(executor, role, adapter, **kwargs):
    request = executor.request(role, "Work", ("Done",), **kwargs)
    return asyncio.run(executor.execute(request, adapter))


def test_allowlisted_import_never_copies_hidden_evaluation_or_credentials(tmp_path):
    executor = make_run(tmp_path)
    files = {p.relative_to(executor.workspace.public).as_posix() for p in executor.workspace.public.rglob("*") if p.is_file()}
    assert files == {"input/instruction.md", "input/starter/public.txt", "input/runtime_config.json"}


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_instruction_bytes_and_prompt_text_preserve_original_newlines(tmp_path, newline):
    source = tmp_path / "task"
    source.mkdir()
    original = newline.join((b"# Original task", b"", b"Make a game.", b""))
    (source / "instruction.md").write_bytes(original)
    workspace, task = Workspace.prepare(tmp_path / "run", source, task_id="original")
    assert workspace.read_public("input/instruction.md").read_bytes() == original
    assert task.instruction.encode("utf-8") == original
    executor = Executor.create(workspace, task, run_id="original", controller="fixed_loop", budget=ResourceLimits())
    assert executor.state.task.instruction.encode("utf-8") == original
    assert "HIDDEN" not in "".join(p.read_text() for p in executor.workspace.public.rglob("*") if p.is_file())


def test_explicit_public_resources_are_copied_without_the_source_tree(tmp_path):
    source = tmp_path / "task"
    source.mkdir()
    (source / "instruction.md").write_text("Task")
    library = tmp_path / "assets"
    library.mkdir()
    (library / "sprite.png").write_bytes(b"sprite")
    workspace, _ = Workspace.prepare(tmp_path / "run", source, task_id="task", resources=(PublicResource("art", library),))
    assert workspace.read_public("input/resources/art/sprite.png").read_bytes() == b"sprite"
    private = source / "tests"
    private.mkdir()
    (private / "rubric.json").write_text("secret")
    with pytest.raises(PermissionDenied):
        Workspace.prepare(tmp_path / "bad", source, task_id="task", resources=(PublicResource("renamed", private),))


def test_windows_reparse_attribute_is_rejected_without_needing_link_privilege(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from adaptive_game_generation.runtime.workspace import no_links
    target = tmp_path / "junction"
    target.mkdir()
    original = Path.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == target:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(PermissionDenied):
        no_links(target)


def test_designer_transaction_commits_artifact_state_and_history_and_reopens(tmp_path):
    executor = make_run(tmp_path)
    outcome = invoke(executor, "designer", design)
    assert outcome.status == "completed"
    state = executor.state
    assert state.state_revision == 1
    assert len(state.ledger.events) == 3
    assert state.ledger.usage.tool_calls == 1
    assert executor.workspace.read_public(state.design.document_ref.path).read_text() == "Player jumps with space."
    assert Executor.open(executor.workspace).state == state
    assert len(list((executor.workspace.private / "history").glob("*.json"))) == 3


@pytest.mark.parametrize("path", ["../.private/state.json", "/etc/passwd", "C:/Users/x/.env", "input/../../tests/rubric.json", "input\\..\\secret", "input/starter/../instruction.md", "CON"])
def test_read_path_escape_is_denied_and_failed_attempt_is_accounted(tmp_path, path):
    executor = make_run(tmp_path)
    async def attack(context):
        await context.tools.read_text(path)
        return result(context)
    outcome = invoke(executor, "designer", attack)
    assert outcome.status == "failed"
    assert outcome.error.code == "permission_denied"
    assert executor.state.state_revision == 0
    assert executor.state.ledger.usage.tool_calls == 1


def test_role_cannot_write_another_roles_committed_files(tmp_path):
    executor = make_run(tmp_path)
    invoke(executor, "designer", design)
    previous = executor.state
    async def attack(context):
        await context.tools.write_text("../developer/game.gd", "tamper")
        return result(context)
    outcome = invoke(executor, "player", attack)
    assert outcome.error.code == "permission_denied"
    assert executor.state.design == previous.design


@pytest.mark.parametrize("role", ["designer", "art_producer", "qa_reviewer"])
def test_only_developer_and_player_can_invoke_process_backend(tmp_path, role):
    executor = make_run(tmp_path)
    async def attack(context):
        await context.tools.run(("python", "-c", "print('not run')"))
        return result(context)
    outcome = invoke(executor, role, attack)
    assert outcome.error.code == "permission_denied"
    assert executor.state.ledger.usage.tool_calls == 0


def test_unconfigured_process_backend_fails_closed(tmp_path):
    executor = make_run(tmp_path)
    async def run(context):
        await context.tools.run(("python", "-c", "print('must not execute on host')"))
        return result(context)
    outcome = invoke(executor, "developer", run)
    assert outcome.error.code == "sandbox_unavailable"
    assert executor.state.ledger.usage.tool_calls == 1


def test_invalid_proposal_rolls_back_files_and_data_but_retains_cost(tmp_path):
    executor = make_run(tmp_path)
    async def invalid(context):
        path = await context.tools.write_text("design.md", "draft")
        ref = ArtifactRef(artifact_id="draft", kind="document", path=path, created_by="designer")
        return result(context, artifact_refs=(ref,), proposed_updates=ProposedUpdates(
            requirements=(Requirement(
                requirement_id="R1", description="Fake", source_excerpt="not in instruction", acceptance_criteria=("Fake",),
            ),),
        ))
    outcome = invoke(executor, "designer", invalid)
    assert outcome.status == "failed"
    assert not executor.state.requirements
    assert not (executor.workspace.public / "artifacts").exists()
    assert executor.state.ledger.usage.tool_calls == 1


def test_player_cannot_publish_game_or_designer_self_approve(tmp_path):
    executor = make_run(tmp_path)
    async def player(context):
        path = await context.tools.write_text("game/project.godot", "text")
        ref = ArtifactRef(artifact_id="game", kind="game_project", path=path.rsplit("/", 1)[0], created_by="player")
        return result(context, artifact_refs=(ref,))
    assert invoke(executor, "player", player).error.code == "permission_denied"
    async def dishonest_designer(context):
        return result(context, proposed_updates=ProposedUpdates(requirements=(Requirement(
            requirement_id="R1", description="Jump", source_excerpt="Player can jump.",
            acceptance_criteria=("Jump",), status="implemented_unverified",
        ),)))
    assert invoke(executor, "designer", dishonest_designer).error.code == "permission_denied"


@pytest.mark.parametrize("limit", ["max_model_calls", "max_input_tokens", "max_output_tokens", "max_tool_calls", "max_tool_seconds", "max_wall_seconds"])
def test_zero_budget_blocks_before_provider_dispatch(tmp_path, limit):
    provider = FakeText()
    executor = make_run(tmp_path, budget=ResourceLimits(**{limit: 0}), services=Services(text=provider))
    async def model(context):
        await context.tools.generate_text("prompt", max_output_tokens=4)
        return result(context)
    outcome = invoke(executor, "designer", model)
    assert outcome.status == "failed"
    assert outcome.error.code == "budget_exceeded"
    assert provider.calls == 0
    assert executor.state.ledger.usage.model_calls == 0


def test_total_wall_deadline_includes_time_between_invocations(tmp_path):
    executor = make_run(tmp_path, budget=ResourceLimits(max_wall_seconds=1.0))
    with executor.store.locked():
        state = executor.store.load()
        executor.store.save(state.revised(ledger=state.ledger.revised(started_at=utc_now() - timedelta(seconds=2))))
    assert invoke(executor, "designer", design).error.code == "budget_exceeded"


def test_per_call_and_total_limits_cannot_be_reset_or_overridden(tmp_path):
    provider = FakeText()
    config = RuntimeConfig(role_limits=ResourceLimits(max_model_calls=1))
    executor = make_run(tmp_path, budget=ResourceLimits(max_model_calls=1), config=config, services=Services(text=provider))
    request = executor.request("designer", "Work", ("Done",)).revised(limits=ResourceLimits(max_model_calls=999))
    with pytest.raises(PermissionDenied):
        asyncio.run(executor.execute(request, design))
    async def model(context):
        await context.tools.generate_text("prompt", max_output_tokens=4)
        return result(context)
    assert invoke(executor, "designer", model).status == "completed"
    assert invoke(executor, "designer", model).error.code == "budget_exceeded"
    assert provider.calls == 1
    assert executor.state.ledger.usage.total_tokens == 8
    assert executor.state.ledger.usage.cached_input_tokens == 2


def test_scheduler_and_worker_share_total_budget_and_scheduler_has_no_write_capability(tmp_path):
    provider = FakeText()
    executor = make_run(tmp_path, controller="adaptive_scheduler", budget=ResourceLimits(max_model_calls=1), services=Services(text=provider))
    async def decide(context):
        await context.tools.generate_text("Choose role", max_output_tokens=4)
        return DecisionProposal(selected_role="designer", rationale="Need a design")
    decision = asyncio.run(executor.decide("decision1", decide))
    assert decision.error is None
    async def model(context):
        await context.tools.generate_text("Write design", max_output_tokens=4)
        return result(context)
    assert invoke(executor, "designer", model).error.code == "budget_exceeded"
    async def attack(context):
        await context.tools.write_text("bad.txt", "forbidden")
        return DecisionProposal(rationale="Stop")
    assert asyncio.run(executor.decide("decision2", attack)).error.code == "permission_denied"
    assert executor.state.ledger.usage.model_calls == 1


def test_provider_failure_is_counted_without_leaking_host_exception_and_blocks_unknown_cost(tmp_path):
    provider = FakeText(failure=True)
    executor = make_run(tmp_path, services=Services(text=provider))
    async def model(context):
        await context.tools.generate_text("prompt", max_output_tokens=4)
        return result(context)
    first = invoke(executor, "designer", model)
    assert first.status == "failed"
    assert executor.state.ledger.usage.model_calls == 1
    assert not executor.state.ledger.usage.token_usage_complete
    assert invoke(executor, "designer", model).error.code == "budget_exceeded"
    assert provider.calls == 1
    assert "SHOULD-NOT-LEAK" not in executor.store.path.read_text()


def test_provider_overrun_is_logged_and_cannot_be_swallowed_by_adapter(tmp_path):
    provider = FakeText(overrun=True)
    executor = make_run(tmp_path, services=Services(text=provider))
    async def model(context):
        try:
            await context.tools.generate_text("prompt", max_output_tokens=4)
        except BudgetExceeded:
            pass
        return result(context)
    outcome = invoke(executor, "designer", model)
    assert outcome.error.code == "budget_exceeded"
    assert executor.state.ledger.usage.input_tokens == 30


def test_timeout_records_failure_and_revokes_capabilities(tmp_path):
    executor = make_run(tmp_path, config=RuntimeConfig(role_limits=ResourceLimits(max_wall_seconds=0.02)))
    captured = []
    async def slow(context):
        captured.append(context.tools)
        await asyncio.sleep(1)
        return result(context)
    outcome = invoke(executor, "designer", slow)
    assert outcome.error.code == "timeout"
    assert executor.state.ledger.usage.wall_seconds >= 0.01
    with pytest.raises(PermissionDenied):
        asyncio.run(captured[0].read_text("input/instruction.md"))


def test_cancellation_preserves_dispatched_model_reservation(tmp_path):
    started = asyncio.Event()
    class SlowText(FakeText):
        async def generate(self, prompt, *, max_output_tokens, timeout):
            self.calls += 1
            started.set()
            await asyncio.sleep(10)
            return TextReply("late", 6, 2)
    provider = SlowText()
    executor = make_run(tmp_path, services=Services(text=provider))
    async def model(context):
        await context.tools.generate_text("prompt", max_output_tokens=4)
        return result(context)
    async def cancel():
        task = asyncio.create_task(executor.execute(executor.request("designer", "Work", ("Done",)), model))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(cancel())
    assert executor.state.ledger.events[-1].payload.result.error.code == "cancelled"
    assert executor.state.ledger.usage.model_calls == 1
    assert not executor.state.ledger.usage.token_usage_complete


def test_background_tool_tasks_are_cancelled_before_the_invocation_returns(tmp_path):
    started, cancelled = asyncio.Event(), []
    class SlowText(FakeText):
        async def generate(self, prompt, *, max_output_tokens, timeout):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
    executor = make_run(tmp_path, services=Services(text=SlowText()))
    async def background(context):
        asyncio.create_task(context.tools.generate_text("prompt", max_output_tokens=4))
        await started.wait()
        return result(context)
    assert invoke(executor, "designer", background).status == "failed"
    assert cancelled == [True]
    assert executor.state.ledger.usage.model_calls == 1


def test_recursive_self_copy_is_rejected(tmp_path):
    executor = make_run(tmp_path)
    async def recursive(context):
        await context.tools.write_text("folder/file", "data")
        await context.tools.copy(context.tools.output_path("folder"), "folder/nested")
        return result(context)
    assert invoke(executor, "designer", recursive).error.code == "permission_denied"


def test_failed_container_cleanup_ends_the_run(tmp_path):
    class BadCleanup:
        async def run(self, workspace, argv, timeout):
            raise SandboxCleanupFailed("Cleanup unconfirmed")
    executor = make_run(tmp_path, services=Services(sandbox=BadCleanup()))
    async def run(context):
        await context.tools.run(("program",))
        return result(context)
    assert invoke(executor, "developer", run).error.code == "sandbox_cleanup_failed"
    assert executor.state.ledger.termination.reason == "failed"
    with pytest.raises(ValueError):
        invoke(executor, "developer", run)


def test_reserved_cost_is_persisted_before_provider_dispatch(tmp_path):
    observed = []
    class InspectingProvider(FakeText):
        async def generate(self, prompt, *, max_output_tokens, timeout):
            journal = json.loads((tmp_path / "generation/.private/inflight.json").read_text())
            observed.append(journal["usage"])
            return TextReply("ok", 6, 2)
    executor = make_run(tmp_path, services=Services(text=InspectingProvider()))
    async def model(context):
        await context.tools.generate_text("prompt", max_output_tokens=4)
        return result(context)
    assert invoke(executor, "designer", model).status == "completed"
    assert observed[0]["model_calls"] == 1
    assert observed[0]["input_tokens"] == 10
    assert not observed[0]["token_usage_complete"]
    assert executor.state.ledger.usage.input_tokens == 6
    assert executor.state.ledger.usage.token_usage_complete


def test_image_permission_metering_and_publication(tmp_path):
    calls = []
    class Images:
        async def generate(self, prompt, *, timeout):
            calls.append(prompt)
            return b"image-fixture"
    executor = make_run(tmp_path, services=Services(image=Images()))
    async def image(context):
        path = await context.tools.generate_image("sprite", "sprite.png")
        return result(context, artifact_refs=(ArtifactRef(artifact_id="sprite", kind="asset", path=path, created_by="art_producer"),))
    assert invoke(executor, "art_producer", image).status == "completed"
    assert executor.state.ledger.usage.image_calls == 1
    assert invoke(executor, "designer", image).error.code == "permission_denied"
    assert calls == ["sprite"]


def test_file_size_and_hash_mismatch_reject_before_publish(tmp_path):
    executor = make_run(tmp_path, config=RuntimeConfig(max_file_bytes=4, max_output_bytes=8))
    async def big(context):
        await context.tools.write_text("big.txt", "too many bytes")
        return result(context)
    assert invoke(executor, "designer", big).error.code == "permission_denied"
    async def wrong_hash(context):
        path = await context.tools.write_text("draft.md", "text")
        return result(context, artifact_refs=(ArtifactRef(
            artifact_id="draft", kind="document", path=path, created_by="designer", sha256="0" * 64,
        ),))
    assert invoke(executor, "designer", wrong_hash).error.code == "invalid_submission"
    assert len(executor.state.artifacts) == 1


def test_case_alias_artifact_references_are_refused(tmp_path):
    executor = make_run(tmp_path)
    async def aliases(context):
        path = await context.tools.write_text("Draft.md", "text")
        first = ArtifactRef(artifact_id="draft1", kind="document", path=path, created_by="designer")
        second = first.revised(artifact_id="draft2", path=path.replace("Draft.md", "draft.md"))
        return result(context, artifact_refs=(first, second))
    assert invoke(executor, "designer", aliases).error.code == "invalid_submission"


def test_runtime_configuration_is_fixed_across_resume(tmp_path):
    executor = make_run(tmp_path)
    with pytest.raises(InvalidSubmission):
        Executor.open(executor.workspace, config=executor.config.revised(max_file_bytes=100))


def test_stale_request_and_multiple_writers_are_rejected(tmp_path):
    executor = make_run(tmp_path)
    stale = executor.request("designer", "Work", ("Done",))
    invoke(executor, "designer", design)
    with pytest.raises(ValueError):
        asyncio.run(executor.execute(stale, design))
    with executor.store.locked():
        with pytest.raises(RunBusy):
            Executor.open(executor.workspace)


def test_recovery_accounts_reservation_and_removes_uncommitted_public_namespace(tmp_path):
    executor = make_run(tmp_path)
    request = executor.request("designer", "Work", ("Done",), request_id="interrupted")
    state = executor.state
    started = Executor._event(state.ledger, RoleCallStarted(request=request))
    with executor.store.locked():
        executor.store.start_journal(request.request_id, request.role.value, 0, utc_now().isoformat())
        executor.store.journal(request.request_id, ResourceUsage(model_calls=1, input_tokens=10, output_tokens=4, token_usage_complete=False))
        executor.store.save(state.revised(ledger=started))
    work = executor.workspace.begin(request.role, request.request_id)
    (work.staging / "draft.md").write_text("uncommitted")
    executor.workspace.publish(work)
    reopened = Executor.open(executor.workspace)
    assert reopened.state.state_revision == 0
    assert reopened.state.ledger.usage.model_calls == 1
    assert not reopened.state.ledger.usage.token_usage_complete
    assert reopened.state.ledger.events[-1].payload.result.error.code == "interrupted"
    assert not (executor.workspace.public / work.prefix).exists()
    assert Executor.open(executor.workspace).state.ledger.usage.model_calls == 1


def test_scheduler_crash_reservation_is_recovered_once(tmp_path):
    executor = make_run(tmp_path, controller="adaptive_scheduler")
    with executor.store.locked():
        executor.store.start_journal("interrupted-decision", "scheduler", 0, utc_now().isoformat())
        executor.store.journal("interrupted-decision", ResourceUsage(model_calls=1, input_tokens=10, token_usage_complete=False))
    reopened = Executor.open(executor.workspace)
    assert reopened.state.ledger.events[-1].payload.error.code == "interrupted"
    assert reopened.state.ledger.usage.model_calls == 1
    assert Executor.open(executor.workspace).state.ledger.usage.model_calls == 1


def test_hidden_directories_and_hardlinks_are_refused_during_import(tmp_path):
    task = tmp_path / "task"
    (task / "workspace" / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text("task")
    (task / "workspace" / "tests" / "rubric.json").write_text("hidden")
    with pytest.raises(PermissionDenied):
        Workspace.prepare(tmp_path / "output", task, task_id="task")
    secret = tmp_path / "secret"
    secret.write_text("hidden")
    alias = tmp_path / "alias"
    os.link(secret, alias)
    with pytest.raises(PermissionDenied):
        inspect_tree(alias)


def test_symlink_escape_is_refused(tmp_path):
    executor = make_run(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("hidden")
    link = executor.workspace.public / "input" / "escape"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("OS does not permit creating symlinks for this user")
    async def attack(context):
        await context.tools.read_text("input/escape")
        return result(context)
    assert invoke(executor, "designer", attack).error.code == "permission_denied"


def test_docker_command_only_mounts_public_and_invocation_output_with_isolation(tmp_path):
    executor = make_run(tmp_path)
    work = executor.workspace.begin("developer", "container")
    backend = DockerSandbox(image="trusted-python@sha256:" + "a" * 64)
    command = backend.command(work, ("python", "-c", "print('ok')"), "test-container")
    joined = " ".join(command)
    assert "--network=none" in command and "--read-only" in command
    assert "--cap-drop=ALL" in command and "--security-opt=no-new-privileges" in command
    assert "--pull=never" in command
    assert str(executor.workspace.private) not in joined
    assert "benchmark" not in joined
    assert "readonly" in command[command.index("--mount") + 1]
    assert command[-2:] == ["-c", "print('ok')"]
    with pytest.raises(ValueError):
        DockerSandbox(image="image", user="0:0")


def test_full_role_cycle_and_new_game_revision_invalidates_old_verification(tmp_path):
    executor = make_run(tmp_path)
    assert invoke(executor, "designer", design).status == "completed"

    async def develop(context):
        version = len(context.state.game_revisions) + 1
        game_id = f"G{version}"
        await context.tools.write_text("game/project.godot", "[application]\n")
        path = await context.tools.write_text("build.txt", "simulated build check for contract test")
        project = ArtifactRef(artifact_id="game", version=version, kind="game_project", path=context.tools.output_path("game"), created_by="developer")
        log = ArtifactRef(artifact_id="build", version=version, kind="log", path=path, created_by="developer")
        build = EvidenceRecord(evidence_id=f"B{version}", kind="build_check", game_revision_id=game_id, artifact_refs=(log,), observation="test fixture", outcome="passed")
        return result(context, artifact_refs=(project, log), proposed_updates=ProposedUpdates(
            requirements=(context.state.requirements[0].revised(status="implemented_unverified", verification=None),),
            game_revision=GameRevision(revision_id=game_id, parent_revision_id=context.state.current_game_revision_id, project_ref=project, change_summary="Implement", build_status="passed", build_evidence_ids=(build.evidence_id,)),
            evidence=(build,),
        ))

    assert invoke(executor, "developer", develop).status == "completed"
    async def play(context):
        path = await context.tools.write_text("jump.json", '{"events": []}')
        ref = ArtifactRef(artifact_id="trace", kind="input_trace", path=path, created_by="player")
        return result(context, artifact_refs=(ref,), proposed_updates=ProposedUpdates(evidence=(EvidenceRecord(
            evidence_id="E1", kind="replay", game_revision_id="G1", requirement_ids=("R1",),
            artifact_refs=(ref,), observation="simulated replay for runtime test", outcome="passed",
        ),)))
    assert invoke(executor, "player", play).status == "completed"
    async def review(context):
        verified = context.state.requirements[0].revised(status="verified_pass", verification=Verification(game_revision_id="G1", evidence_ids=("E1",), rationale="Fixture evidence"))
        return result(context, proposed_updates=ProposedUpdates(requirements=(verified,), readiness=ReadinessReview(game_revision_id="G1", ready=True, rationale="Fixture ready", evidence_ids=("E1",))))
    assert invoke(executor, "qa_reviewer", review).status == "completed"
    previous = executor.state
    assert previous.requirements[0].status == "verified_pass"
    assert invoke(executor, "developer", develop).status == "completed"
    current = executor.state
    assert current.current_game_revision_id == "G2"
    assert current.requirements[0].status == "implemented_unverified"
    assert current.readiness is None
    assert all(record in current.evidence for record in previous.evidence)
    assert executor.workspace.read_public(previous.game_revisions[0].project_ref.path).is_dir()
    with pytest.raises(InvalidSubmission):
        executor.finish("completed", "Not yet verified")
    state = executor.finish("budget_exhausted", "Stop")
    assert state.ledger.termination.reason == "budget_exhausted"


@pytest.mark.skipif(not os.environ.get("GAMECRAFT_SANDBOX_IMAGE"), reason="Requires a running Docker daemon and an explicitly supplied local Python image")
def test_real_container_cannot_access_private_files_or_write_committed_input(tmp_path):
    executor = make_run(tmp_path)
    work = executor.workspace.begin("developer", "real-container")
    (executor.workspace.private / "sentinel").write_text("HIDDEN")
    backend = DockerSandbox(image=os.environ["GAMECRAFT_SANDBOX_IMAGE"])
    script = (
        "from pathlib import Path; "
        "assert not Path('/workspace/.private/sentinel').exists(); "
        "assert not Path('/workspace/tests/rubric.json').exists(); "
        "p=Path('/workspace/input/instruction.md'); "
        "print(p.read_text()); "
        "p.write_text('tampered')"
    )
    outcome = asyncio.run(backend.run(work, ("python", "-c", script), 20.0))
    assert outcome.returncode != 0
    assert "Read-only file system" in outcome.stderr or "Permission denied" in outcome.stderr
    assert executor.workspace.read_public("input/instruction.md").read_text() == "Make a game. Player can jump."
