"""Execute the real chat SDK against an offline transport; no credentials/network."""
import asyncio
import json

import pytest

from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.providers.openai_compatible import ProviderFailure
from test_provider import completion, settings

httpx = pytest.importorskip("httpx")


@pytest.mark.parametrize("dialect", ["native", "alibaba", "unspecified"])
def test_per_call_profiles_keep_deep_settings_and_never_mutate_thinking_or_output_allowance(tmp_path, dialect):
    calls = []
    config = settings(developer_profiles=True, reasoning_effort="high", max_output_tokens=131072,
        **({"thinking": "enabled"} if dialect == "native" else {"enable_thinking": True} if dialect == "alibaba" else {}))
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=completion())
    async def run():
        async with OpenAICompatibleTextProvider(config, capture_directory=tmp_path / "calls",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as provider:
            for profile in ("deep", "local_repair", "finalize", "deep"):
                await provider.generate_profiled('{"instruction":"Offline transport contract"}', profile=profile,
                                                max_output_tokens=131072, timeout=20)
            assert provider.config == config and provider.call_count == 4
    asyncio.run(run())
    assert [c["reasoning_effort"] for c in calls] == ["high", "medium", "low", "high"]
    assert all(c["max_tokens"] == 131072 and c["model"] == config.model for c in calls)
    assert all(c.get("enable_thinking") == (True if dialect == "alibaba" else None) for c in calls)
    assert all(c.get("thinking") == ({"type": "enabled"} if dialect == "native" else None) for c in calls)
    for i, call in enumerate(calls, 1):
        assert json.loads((tmp_path / f"calls/{i:03}/request.json").read_text()) == call
        metadata = json.loads((tmp_path / f"calls/{i:03}/metadata.json").read_text())
        assert metadata["developer_profile"]["changed"] == (i in (2, 3))
        assert "SECRET-TEST-KEY" not in json.dumps(metadata)


def test_toggle_only_gateways_keep_deep_thinking_without_unsupported_effort_parameter(tmp_path):
    captured = []
    async def run():
        config = settings(developer_profiles=True, thinking="enabled")
        async with OpenAICompatibleTextProvider(config, capture_directory=tmp_path / "calls",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (
                captured.append(json.loads(r.content)) or httpx.Response(200, json=completion()))))) as provider:
            await provider.generate_profiled("{}", profile="local_repair", max_output_tokens=100, timeout=10)
            assert not provider.profile_settings("local_repair")["changed"]
    asyncio.run(run())
    assert "reasoning_effort" not in captured[0] and captured[0]["thinking"] == {"type": "enabled"}


def test_profile_env_is_developer_only_and_can_preserve_full_effort(tmp_path):
    env = tmp_path / ".env"
    env.write_text("CODE_DEVELOPER_MODEL=fixture\nDESIGNER_MODEL=fixture\nMODEL_GATEWAY_API_KEY=unused\n"
        "MODEL_GATEWAY_BASE_URL=https://unused.test/v1\nCODE_DEVELOPER_REASONING_EFFORT=high\n"
        "CODE_DEVELOPER_LOCAL_REASONING_EFFORT=inherit\nCODE_DEVELOPER_FINALIZE_REASONING_EFFORT=medium\n")
    dev = GatewayConfig.from_env(env, prefix="CODE_DEVELOPER", environ={})
    assert dev.developer_profiles and dev.developer_local_effort == "inherit"
    assert dev.developer_finalize_effort == "medium"
    assert "developer_profiles" not in GatewayConfig.from_env(env, environ={}).public_settings()
    disabled = GatewayConfig.from_env(env, prefix="CODE_DEVELOPER", environ={"CODE_DEVELOPER_OPTIMIZE_EXECUTION": "false"})
    assert not disabled.developer_profiles and disabled.configuration_id != dev.configuration_id
    for invalid in ({"CODE_DEVELOPER_OPTIMIZE_EXECUTION": "perhaps"}, {"CODE_DEVELOPER_LOCAL_REASONING_EFFORT": "secret-mode"}):
        with pytest.raises(ProviderFailure):
            GatewayConfig.from_env(env, prefix="CODE_DEVELOPER", environ=invalid)


def test_profile_failure_has_one_attempt_and_retains_actual_wire_settings(tmp_path):
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "Unsupported fixture option"}})
    async def run():
        async with OpenAICompatibleTextProvider(settings(developer_profiles=True, reasoning_effort="high"),
            capture_directory=tmp_path / "calls", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as provider:
            with pytest.raises(ProviderFailure, match="HTTP 400"):
                await provider.generate_profiled("{}", profile="local_repair", max_output_tokens=200, timeout=10)
    asyncio.run(run())
    assert len(calls) == 1 and calls[0]["reasoning_effort"] == "medium"
    assert json.loads((tmp_path / "calls/001/metadata.json").read_text())["status"] == "failed"
