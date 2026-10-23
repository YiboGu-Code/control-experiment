"""Verify live-runner wiring using offline HTTP and engine fixtures only."""

import asyncio
import json
import os
import runpy
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("openai")
pytest.importorskip("dotenv")

from adaptive_game_generation.controllers import FixedLoop, FixedLoopConfig
from adaptive_game_generation.controllers.results import export_run
from adaptive_game_generation.providers import DashScopeImageConfig, DashScopeImageProvider
from adaptive_game_generation.roles.art_producer import ArtManifest
from test_fixed_loop import make_run, report
from test_image_provider import image_response, job, provider
from test_dashscope_image import sdk_response, image_bytes

ROOT = Path(__file__).resolve().parents[1]


def test_real_provider_boundary_integrates_art_and_charges_the_shared_ledger(tmp_path):
    service = provider(tmp_path, lambda r: image_response())
    executor, adapters, _, _ = make_run(tmp_path, image_service=service)
    async def run():
        async with service:
            return await FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run()
    state = asyncio.run(run())
    art = report(executor, "art-manifest", ArtManifest)
    assert not art.assets[0].is_mock
    assert art.assets[0].record.source.startswith("generated:live-image-")
    assert state.ledger.usage.image_calls == 1 and len(service.calls) == 1
    assert art.assets[0].record.artifact_ref.sha256 == service.calls[0]["sha256"]
    assert state.game_revisions  # Developer consumed the actual normalized PNG.
    export_run(executor, tmp_path / "review", mode="fixed-loop/real-text-models/real-image-generation/real-isolated-engine")
    review = (tmp_path / "review/review.md").read_text(encoding="utf-8")
    assert "configured to use the real image service" in review
    assert "Image generation is mock." not in review


def test_failed_image_request_still_counts_and_never_publishes_mock_assets(tmp_path):
    service = provider(tmp_path, lambda r: httpx.Response(503, json={"error": "temporarily unavailable"}))
    executor, adapters, _, _ = make_run(tmp_path, image_service=service)
    async def run():
        async with service:
            return await FixedLoop(executor, adapters, FixedLoopConfig(max_cycles=1)).run()
    state = asyncio.run(run())
    assert state.ledger.usage.image_calls == 1 and len(service.calls) == 1
    assert not state.assets and not state.game_revisions
    assert service.calls[0]["status"] == "failed"


def runner_settings(tmp_path, monkeypatch, *, with_image=True):
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    env = tmp_path / "test.env"
    settings = {"MODEL_GATEWAY_API_KEY": "RUNNER-SECRET",
                "MODEL_GATEWAY_BASE_URL": "https://runner.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"}
    settings.update({prefix + "_MODEL": "fixture-text-model" for prefix in runner["PREFIXES"].values()})
    if with_image:
        settings["ART_IMAGE_MODEL"] = "fixture-image-model"
        settings["ART_IMAGE_TIMEOUT_SECONDS"] = "701"
    for name in list(os.environ):
        if name.startswith(("MODEL_GATEWAY_", "DASHSCOPE_", "ART_IMAGE_", "ART_KENNEY_", "ART_OGA_", "ART_LIBRARY_",
                            "FIXED_LOOP_", "RUN_MAX_WALL_SECONDS", "DEVELOPER_VISUAL_")) or any(
                name.startswith(prefix + "_") for prefix in runner["PREFIXES"].values()):
            monkeypatch.delenv(name)
    env.write_text("\n".join(f"{key}={value}" for key, value in settings.items()) + "\n")
    monkeypatch.setitem(runner["run_live"].__globals__, "engine_settings",
        lambda values: {"developer": {"image_id": "offline-engine"}, "player": {"image_id": "offline-player"}})
    return runner, env


def test_preflight_uses_env_image_model_without_network(tmp_path, monkeypatch):
    runner, env = runner_settings(tmp_path, monkeypatch)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not send any model/image/download request")
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    output = tmp_path / "preflight"
    result = asyncio.run(runner["run_live"](output, env_file=env, preflight_only=True))
    assert result["image_mode"] == "real" and not result["models_called"]
    saved = json.loads((output / "experiment.json").read_bytes())
    assert saved["image_configuration"]["model"] == "fixture-image-model"
    assert not saved["images"]["is_mock"]
    assert saved["image_configuration"]["adapter"] == "dashscope-multimodal-images.v1"
    assert saved["image_configuration"]["timeout_seconds"] == 701
    assert saved["roles"]["developer"]["prompt_version"] == "developer.v3"
    assert saved["roles"]["developer"]["optimize_execution"] is True
    assert saved["roles"]["developer"]["max_model_turns"] == 8
    assert saved["role_limits"]["developer"]["max_model_calls"] == 8
    assert "RUNNER-SECRET" not in (output / "experiment.json").read_text(encoding="utf-8")
    assert "runner.cn-beijing" not in (output / "experiment.json").read_text(encoding="utf-8")
    assert not (output / "image_calls").exists()


def test_missing_image_model_fails_before_any_text_call_and_mock_requires_explicit_flag(tmp_path, monkeypatch):
    from adaptive_game_generation.providers.openai_compatible import ProviderFailure
    runner, env = runner_settings(tmp_path, monkeypatch, with_image=False)
    with pytest.raises(ProviderFailure, match="ART_IMAGE_MODEL"):
        asyncio.run(runner["run_live"](tmp_path / "missing", env_file=env, preflight_only=True))
    result = asyncio.run(runner["run_live"](tmp_path / "explicit-mock", env_file=env,
                                         mock_images=True, preflight_only=True))
    assert result["image_mode"] == "mock"
    assert json.loads((tmp_path / "explicit-mock/experiment.json").read_bytes())["images"]["is_mock"]


@pytest.mark.parametrize("explicit,process,expected", [(None, None, 21600), (None, "28800", 28800), (900, "28800", 900)])
def test_shared_wall_budget_precedence_is_frozen_without_online_calls(tmp_path, monkeypatch, explicit, process, expected):
    runner, env = runner_settings(tmp_path, monkeypatch)
    assert runner["run_wall_seconds"](None, {}) == 43200
    with env.open("a") as stream:
        stream.write("RUN_MAX_WALL_SECONDS=21600\n")
    if process:
        monkeypatch.setenv("RUN_MAX_WALL_SECONDS", process)
    output = tmp_path / "deadline"
    asyncio.run(runner["run_live"](output, env_file=env, preflight_only=True, max_wall_seconds=explicit))
    budget = json.loads((output / "experiment.json").read_bytes())["total_budget"]
    assert budget["max_wall_seconds"] == budget["max_tool_seconds"] == expected


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "invalid"])
def test_invalid_run_wall_deadline_is_rejected(value):
    runner = runpy.run_path(str(ROOT / "examples/fixed_loop_demo.py"))
    with pytest.raises(ValueError):
        runner["run_wall_seconds"](None, {"RUN_MAX_WALL_SECONDS": value})


@pytest.mark.parametrize("godot_mcp", [False, True])
def test_expanded_thinking_settings_reach_frozen_role_and_total_budgets(tmp_path, monkeypatch, godot_mcp):
    runner, env = runner_settings(tmp_path, monkeypatch)
    settings = {
        "DESIGNER": (65536, 1048576), "ART_PLANNER": (65536, 1048576),
        "CODE_DEVELOPER": (131072, 4194304), "PLAYER": (32768, 1572864), "QA": (65536, 2097152),
    }
    with env.open("a") as stream:
        for prefix, (tokens, prompt_bytes) in settings.items():
            stream.write(f"{prefix}_MAX_OUTPUT_TOKENS={tokens}\n{prefix}_MAX_PROMPT_BYTES={prompt_bytes}\n"
                         f"{prefix}_ENABLE_THINKING=true\n{prefix}_STREAM=true\n{prefix}_TIMEOUT_SECONDS=1800\n")
        stream.write("QA_VISION=true\nDEVELOPER_VISUAL_MAX_OUTPUT_TOKENS=32768\n"
                     "FIXED_LOOP_ROLE_MAX_INPUT_TOKENS=40000000\n"
                     "FIXED_LOOP_MAX_INPUT_TOKENS=128000000\nFIXED_LOOP_MAX_OUTPUT_TOKENS=8000000\n")
    # Process settings must override the selected .env, just like gateway settings.
    monkeypatch.setenv("PLAYER_MAX_PROMPT_BYTES", "2000000")
    monkeypatch.setenv("FIXED_LOOP_MAX_OUTPUT_TOKENS", "9000000")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Configuration verification must never call online models")
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    output = tmp_path / "expanded"
    asyncio.run(runner["run_live"](output, env_file=env, preflight_only=True, godot_mcp=godot_mcp,
                                  max_model_calls=64, max_wall_seconds=43200.0))
    saved = json.loads((output / "experiment.json").read_bytes())
    for role, prefix in runner["PREFIXES"].items():
        provider, config, budget = saved["providers"][role], saved["roles"][role], saved["role_limits"][role]
        tokens, prompt_bytes = settings[prefix]
        assert provider["max_output_tokens"] == config["max_output_tokens"] == tokens
        assert provider["enable_thinking"] and provider["stream"]
        assert provider["timeout_seconds"] == 1800
        assert config["max_prompt_bytes"] == (2000000 if prefix == "PLAYER" else prompt_bytes)
        assert budget["max_input_tokens"] == 40000000
    developer = saved["roles"]["developer"]
    budget = saved["role_limits"]["developer"]
    assert budget["max_output_tokens"] == developer["max_model_turns"] * 131072 + (3 * 32768 if godot_mcp else 0)
    if godot_mcp:
        visual = saved["godot_mcp"]["visual_provider"]
        assert visual["enable_thinking"] and visual["stream"] and visual["max_output_tokens"] == 32768
    total = saved["total_budget"]
    assert (total["max_input_tokens"], total["max_output_tokens"]) == (128000000, 9000000)
    assert total["max_model_calls"] == 64 and total["max_wall_seconds"] == 43200


@pytest.mark.parametrize("setting", ["CODE_DEVELOPER_MAX_PROMPT_BYTES=0", "FIXED_LOOP_MAX_OUTPUT_TOKENS=-1",
                                    "FIXED_LOOP_ROLE_MAX_INPUT_TOKENS=invalid"])
def test_invalid_expanded_limits_fail_during_preflight(tmp_path, monkeypatch, setting):
    runner, env = runner_settings(tmp_path, monkeypatch)
    with env.open("a") as stream:
        stream.write(setting + "\n")
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        asyncio.run(runner["run_live"](output, env_file=env, preflight_only=True))
    assert json.loads((output / "summary.json").read_bytes())["status"] == "preflight_failed"
    assert not (output / "model_calls").exists()


def test_runner_injects_image_service_timeout_and_preserves_records_on_failure(tmp_path, monkeypatch):
    runner, env = runner_settings(tmp_path, monkeypatch)
    instances = []
    def create_image(config, *, capture_directory):
        assert isinstance(config, DashScopeImageConfig)
        assert config.base_url.get_secret_value().endswith("/api/v1")
        async def call(**kwargs):
            return sdk_response()
        service = DashScopeImageProvider(config, capture_directory=capture_directory, sdk_call=call,
            download_client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=image_bytes()))))
        instances.append(service)
        return service
    class TestLoop:
        def __init__(self, executor, *args, **kwargs):
            self.executor = executor
        async def run(self):
            assert self.executor.config.tool_timeout_seconds >= 701
            assert self.executor.services_by_role["art_producer"].image is instances[0]
            assert not self.executor.services_by_role["art_producer"].image_info.is_mock
            assert "mock placeholders" not in " ".join(self.executor.state.task.environment_constraints)
            async def interrupted_art(context):
                await context.tools.generate_image(job().model_dump_json(), "assets/test.png")
                raise RuntimeError("Offline simulated host interruption")
            await self.executor.execute(self.executor.request("art_producer", "Test the image boundary", ("Record image usage",)),
                                        interrupted_art)
            raise RuntimeError("Offline simulated runner failure")
    monkeypatch.setitem(runner["run_live"].__globals__, "create_image_provider", create_image)
    monkeypatch.setitem(runner["run_live"].__globals__, "FixedLoop", TestLoop)
    output = tmp_path / "interrupted"
    with pytest.raises(RuntimeError, match="runner failure"):
        asyncio.run(runner["run_live"](output, env_file=env))
    assert json.loads((output / "image_calls.json").read_bytes())[0]["status"] == "completed"
    summary = json.loads((output / "summary.json").read_bytes())
    assert summary["usage"]["image_calls"] == 1
    assert "/real-image-generation/" in summary["mode"]
    assert instances[0]._client.is_closed and instances[0]._downloads.is_closed
