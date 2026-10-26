"""Transport/usage boundary unit tests; actual role integration is test_designer_live."""

from __future__ import annotations

import asyncio
import json

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("openai")
pytest.importorskip("dotenv")

from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.providers.openai_compatible import ProviderFailure, SYSTEM_PROMPT


def settings(**changes):
    return GatewayConfig(
        model="test-model", api_key="SECRET-TEST-KEY", base_url="https://gateway.test/v1",
        **changes,
    )


def test_stream_heartbeats_do_not_extend_total_request_deadline(tmp_path):
    closed = []
    class EndlessStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": heartbeat\n\n"
                await asyncio.sleep(0.01)
        async def aclose(self):
            closed.append(True)
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=EndlessStream(), headers={"content-type": "text/event-stream"})
    async def run():
        async with OpenAICompatibleTextProvider(settings(stream=True, timeout_seconds=2.0),
                capture_directory=tmp_path / "calls", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as provider:
            with pytest.raises(ProviderFailure, match="timed out"):
                await asyncio.wait_for(provider.generate("Return JSON", max_output_tokens=128, timeout=30), 8)
    asyncio.run(run())
    meta = json.loads((tmp_path / "calls/001/metadata.json").read_bytes())
    assert meta["error_code"] == "provider_timeout" and "usage" not in meta
    assert len(calls) == 1 and closed


def completion(*, text='{"answer":"example"}', usage=None):
    return {
        "id": "completion-test", "object": "chat.completion", "created": 1, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": usage or {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120, "prompt_tokens_details": {"cached_tokens": 25}},
    }


def test_explicit_env_loading_precedence_and_secret_exclusion(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DESIGNER_MODEL=file-model\nMODEL_GATEWAY_API_KEY=SECRET-FILE\nMODEL_GATEWAY_BASE_URL=https://gateway.test/v1\n", encoding="utf-8")
    config = GatewayConfig.from_env(env, environ={"DESIGNER_MODEL": "process-model", "DESIGNER_REASONING_EFFORT": "low"})
    assert config.model == "process-model"
    assert config.reasoning_effort == "low"
    assert config.api_key.get_secret_value() == "SECRET-FILE"
    assert "SECRET-FILE" not in repr(config) + json.dumps(config.public_settings())
    assert "gateway.test" not in repr(config) + json.dumps(config.public_settings())
    assert config.public_settings()["max_retries"] == 0
    assert config.configuration_id == config.revised(api_key="NEW-KEY").configuration_id
    assert config.configuration_id != config.revised(reasoning_effort="high").configuration_id


def test_art_planner_uses_its_own_settings_and_shared_secret_gateway(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DESIGNER_MODEL=designer\nART_PLANNER_MODEL=art\nART_PLANNER_MAX_OUTPUT_TOKENS=32768\n"
        "ART_PLANNER_REASONING_EFFORT=low\nMODEL_GATEWAY_API_KEY=SECRET\nMODEL_GATEWAY_BASE_URL=https://gateway.test\n",
        encoding="utf-8")
    config = GatewayConfig.from_env(env, environ={}, prefix="ART_PLANNER")
    assert config.model == "art" and config.max_output_tokens == 32768 and config.reasoning_effort == "low"
    assert "SECRET" not in repr(config) + json.dumps(config.public_settings())
    assert GatewayConfig.from_env(env, environ={}).model == "designer"


def test_developer_model_configuration_is_separate(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DESIGNER_MODEL=designer\nART_PLANNER_MODEL=art\nCODE_DEVELOPER_MODEL=developer\n"
        "CODE_DEVELOPER_MAX_OUTPUT_TOKENS=32768\nCODE_DEVELOPER_REASONING_EFFORT=low\nCODE_DEVELOPER_THINKING=disabled\n"
        "MODEL_GATEWAY_API_KEY=SECRET\nMODEL_GATEWAY_BASE_URL=https://gateway.test\n", encoding="utf-8")
    config = GatewayConfig.from_env(env, environ={}, prefix="CODE_DEVELOPER")
    assert config.model == "developer" and config.max_output_tokens == 32768 and config.reasoning_effort == "low"
    assert config.thinking == "disabled" and config.public_settings()["thinking"] == "disabled"
    assert config.configuration_id != config.revised(thinking=None).configuration_id
    assert "SECRET" not in repr(config) + json.dumps(config.public_settings())
    assert GatewayConfig.from_env(env, environ={}, prefix="ART_PLANNER").model == "art"


@pytest.mark.parametrize("name,value", [
    ("DESIGNER_TIMEOUT_SECONDS", "nan"), ("DESIGNER_MAX_OUTPUT_TOKENS", "0"),
    ("DESIGNER_JSON_MODE", "maybe"), ("DESIGNER_TOKEN_PARAMETER", "unsupported"),
    ("DESIGNER_REASONING_EFFORT", "unsupported"),
    ("DESIGNER_THINKING", "unsupported"),
    ("DESIGNER_ENABLE_THINKING", "maybe"),
    ("DESIGNER_ENABLE_THINKING", ""),
])
def test_invalid_settings_are_rejected_without_echoing_credentials(tmp_path, name, value):
    env = tmp_path / ".env"
    env.write_text("DESIGNER_MODEL=test\nMODEL_GATEWAY_API_KEY=SECRET\nMODEL_GATEWAY_BASE_URL=https://gateway.test\n", encoding="utf-8")
    with pytest.raises(ProviderFailure) as error:
        GatewayConfig.from_env(env, environ={name: value})
    assert "SECRET" not in str(error.value)


@pytest.mark.parametrize("prefix", ["DESIGNER", "ART_PLANNER", "CODE_DEVELOPER", "PLAYER", "QA", "ART_SVG", "SCHEDULER"])
def test_alibaba_role_settings_are_loaded_without_other_role_leakage(tmp_path, prefix):
    env = tmp_path / ".env"
    env.write_text(f"{prefix}_MODEL=qwen3.8-flash\n{prefix}_ENABLE_THINKING=false\n"
                   "MODEL_GATEWAY_API_KEY=SECRET\nMODEL_GATEWAY_BASE_URL=https://gateway.test\n", encoding="utf-8")
    config = GatewayConfig.from_env(env, environ={}, prefix=prefix)
    assert config.enable_thinking is False
    assert config.thinking is None
    assert config.public_settings()["enable_thinking"] is False
    assert config.configuration_id != config.revised(enable_thinking=True).configuration_id


@pytest.mark.parametrize("enabled", [False, True])
def test_alibaba_thinking_wire_parameter_and_capture(tmp_path, enabled):
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    async def run():
        async with OpenAICompatibleTextProvider(
            settings(enable_thinking=enabled), capture_directory=tmp_path / "calls",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        ) as provider:
            await provider.generate('{"task":"Check JSON transport"}', max_output_tokens=128, timeout=5)

    asyncio.run(run())
    saved = json.loads((tmp_path / "calls/001/request.json").read_text(encoding="utf-8"))
    assert calls == [saved]
    assert saved["enable_thinking"] is enabled
    assert "thinking" not in saved and "extra_body" not in saved


def test_thinking_dialects_cannot_silently_override_each_other(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DESIGNER_MODEL=test\nDESIGNER_THINKING=disabled\nDESIGNER_ENABLE_THINKING=false\n"
                   "MODEL_GATEWAY_API_KEY=SECRET\nMODEL_GATEWAY_BASE_URL=https://gateway.test\n", encoding="utf-8")
    with pytest.raises(ProviderFailure, match="Invalid text-role"):
        GatewayConfig.from_env(env, environ={})


def test_unset_alibaba_extension_preserves_existing_configuration_identity():
    config = settings()
    public = config.public_settings()
    assert "enable_thinking" not in public and "thinking" not in public
    assert config.revised(timeout_seconds=60).enable_thinking is None


@pytest.mark.parametrize("token_parameter", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("reasoning_effort", [None, "low"])
@pytest.mark.parametrize("thinking", [None, "enabled", "disabled"])
def test_real_sdk_request_mapping_usage_capture_and_exact_text(tmp_path, token_parameter, reasoning_effort, thinking):
    calls = []
    text = '{\n  "answer": "English with a newline"\n}\n'

    def handle(request):
        calls.append(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer SECRET-TEST-KEY"
        return httpx.Response(200, json=completion(text=text))

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        async with OpenAICompatibleTextProvider(settings(token_parameter=token_parameter, reasoning_effort=reasoning_effort, thinking=thinking), capture_directory=tmp_path / "calls", http_client=client) as provider:
            prompt = '{"instruction":"Design a game"}'
            assert provider.input_token_upper_bound(prompt) > len((prompt + SYSTEM_PROMPT).encode())
            reply = await provider.generate(prompt, max_output_tokens=128, timeout=5.0)
            assert (reply.text, reply.input_tokens, reply.output_tokens, reply.cached_input_tokens) == (text, 100, 20, 25)
            assert provider.call_count == 1
        assert client.is_closed

    asyncio.run(run())
    saved = json.loads((tmp_path / "calls/001/request.json").read_text(encoding="utf-8"))
    assert saved == calls[0]
    assert saved[token_parameter] == 128 and saved["response_format"] == {"type": "json_object"}
    assert saved["messages"][0]["content"] == SYSTEM_PROMPT
    assert saved.get("reasoning_effort") == reasoning_effort
    assert ("reasoning_effort" in saved) == (reasoning_effort is not None)
    assert saved.get("thinking") == ({"type": thinking} if thinking is not None else None)
    assert (tmp_path / "calls/001/response.txt").read_bytes() == text.encode("utf-8")
    for path in (tmp_path / "calls/001").iterdir():
        assert "SECRET-TEST-KEY" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("failure", ["http", "connection", "timeout"])
def test_transport_failure_is_sanitized_and_never_automatically_retried(tmp_path, failure):
    calls = []

    def handle(request):
        calls.append(request)
        if failure == "http":
            return httpx.Response(500, json={"error": {"message": "SECRET-TEST-KEY", "type": "server_error"}})
        if failure == "connection":
            raise httpx.ConnectError("SECRET-TEST-KEY", request=request)
        raise httpx.ReadTimeout("SECRET-TEST-KEY", request=request)

    async def run():
        async with OpenAICompatibleTextProvider(settings(), capture_directory=tmp_path / "calls", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as provider:
            with pytest.raises(ProviderFailure) as error:
                await provider.generate("{}", max_output_tokens=128, timeout=5.0)
            assert "SECRET-TEST-KEY" not in str(error.value)

    asyncio.run(run())
    assert len(calls) == 1
    metadata = json.loads((tmp_path / "calls/001/metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert not (tmp_path / "calls/001/response.json").exists()


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_invalid_usage_keeps_response_but_does_not_invent_counts(tmp_path, missing):
    body = completion()
    body["usage"] = None if missing else {
        "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 2},
    }

    async def run():
        async with OpenAICompatibleTextProvider(settings(), capture_directory=tmp_path / "calls", http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))) as provider:
            with pytest.raises(ProviderFailure, match="usage"):
                await provider.generate("{}", max_output_tokens=128, timeout=5.0)

    asyncio.run(run())
    assert (tmp_path / "calls/001/response.json").exists()


def test_cancellation_reaches_async_transport_and_closes_client(tmp_path):
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()

        async def handle(request):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        async with OpenAICompatibleTextProvider(settings(), capture_directory=tmp_path / "calls", http_client=client) as provider:
            task = asyncio.create_task(provider.generate("{}", max_output_tokens=128, timeout=5.0))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cancelled.is_set()
        assert client.is_closed

    asyncio.run(run())
    metadata = json.loads((tmp_path / "calls/001/metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "cancelled"


def test_unexpected_secret_echo_is_redacted_from_capture(tmp_path):
    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json=completion(text='{"echo":"SECRET-TEST-KEY"}')))
        async with OpenAICompatibleTextProvider(settings(), capture_directory=tmp_path / "calls", http_client=httpx.AsyncClient(transport=transport)) as provider:
            await provider.generate("{}", max_output_tokens=128, timeout=5.0)

    asyncio.run(run())
    for path in (tmp_path / "calls/001").iterdir():
        assert "SECRET-TEST-KEY" not in path.read_text(encoding="utf-8")
    assert "[REDACTED]" in (tmp_path / "calls/001/response.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("complete", [True, False])
def test_stream_preserves_chunks_text_and_usage_without_repair(tmp_path, complete):
    pieces = ['{\n', '"answer": "English"', '\n}']
    chunks = [{
        "id": "stream-test", "object": "chat.completion.chunk", "created": 1, "model": "test-model",
        "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
    } for piece in pieces]
    if complete:
        chunks.extend([
            {**chunks[0], "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {**chunks[0], "choices": [], "usage": completion()["usage"]},
        ])
    wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"

    def handle(request):
        payload = json.loads(request.content)
        assert payload["stream"] and payload["stream_options"] == {"include_usage": True}
        assert request.extensions["timeout"]["connect"] == 10.0
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        async with OpenAICompatibleTextProvider(settings(stream=True), capture_directory=tmp_path / "calls", http_client=client) as provider:
            if complete:
                reply = await provider.generate("{}", max_output_tokens=128, timeout=60)
                assert (reply.text, reply.input_tokens, reply.output_tokens, reply.cached_input_tokens) == ("".join(pieces), 100, 20, 25)
            else:
                with pytest.raises(ProviderFailure, match="completion marker"):
                    await provider.generate("{}", max_output_tokens=128, timeout=60)
        assert client.is_closed

    asyncio.run(run())
    captured = [json.loads(line) for line in (tmp_path / "calls/001/response_chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    assert captured == chunks
    assert (tmp_path / "calls/001/response.txt").read_bytes() == "".join(pieces).encode("utf-8")


def test_cancelled_stream_closes_connection_and_preserves_received_chunks(tmp_path):
    async def run():
        entered = asyncio.Event()

        class Body(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                chunk = {"id": "partial", "object": "chat.completion.chunk", "created": 1,
                         "model": "test-model", "choices": [{"index": 0, "delta": {"content": "{"}, "finish_reason": None}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                entered.set()
                await asyncio.Future()

            async def aclose(self):
                self.closed = True

        body = Body()
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"}))
        async with OpenAICompatibleTextProvider(settings(stream=True), capture_directory=tmp_path / "calls", http_client=httpx.AsyncClient(transport=transport)) as provider:
            task = asyncio.create_task(provider.generate("{}", max_output_tokens=128, timeout=60))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert body.closed

    asyncio.run(run())
    meta = json.loads((tmp_path / "calls/001/metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "cancelled" and meta["stream_chunks"] == 1
    assert len((tmp_path / "calls/001/response_chunks.jsonl").read_text(encoding="utf-8").splitlines()) == 1
    assert not (tmp_path / "calls/001/response.json").exists()


def test_thinking_stream_preserves_reasoning_separately_and_meters_total_output(tmp_path):
    common = {"id": "thinking-test", "object": "chat.completion.chunk", "created": 1, "model": "test-model"}
    chunks = [
        {**common, "choices": [{"index": 0, "delta": {"reasoning_content": "Synthetic reasoning fixture."}, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {"content": '{"answer":"English"}'}, "finish_reason": None}]},
        {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {**common, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                                          "completion_tokens_details": {"reasoning_tokens": 30}}},
    ]
    wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    def handle(request):
        body = json.loads(request.content)
        assert body["enable_thinking"] is True and body["reasoning_effort"] == "high"
        assert "thinking" not in body
        assert body["stream"] and body["response_format"] == {"type": "json_object"}
        return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream"})
    async def run():
        async with OpenAICompatibleTextProvider(settings(stream=True, enable_thinking=True, reasoning_effort="high"),
                capture_directory=tmp_path / "calls", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle))) as provider:
            reply = await provider.generate("Return English JSON.", max_output_tokens=128, timeout=5)
            assert reply.text == '{"answer":"English"}' and reply.output_tokens == 40
    asyncio.run(run())
    response = json.loads((tmp_path / "calls/001/response.json").read_bytes())
    assert response["choices"][0]["message"]["reasoning_content"] == "Synthetic reasoning fixture."
    assert response["usage"]["completion_tokens_details"]["reasoning_tokens"] == 30
    metadata = json.loads((tmp_path / "calls/001/metadata.json").read_bytes())
    assert metadata["reasoning_present"] and metadata["finish_reason"] == "stop"
