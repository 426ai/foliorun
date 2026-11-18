"""Installed vendor clients, controlled transport. These do NOT claim real Server/model success."""

from types import SimpleNamespace

import httpx
import pytest
from opensandbox.adapters.filesystem_adapter import FilesystemAdapter
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint
from pydantic_ai import Agent

from foliorun import RuntimeFault
from foliorun.adapters.opensandbox import download_binary
from foliorun.adapters.pydantic_ai import PydanticHarness, openrouter_model
from foliorun.contracts import MODEL, digest


async def test_sdk_0116_complete_binary_no_line_parameters(monkeypatch):
    raw = b"PK\x03\x04\x00\xff\n\x00\r\n" + bytes(range(256)) * 600
    requests = []

    def serve(request):
        requests.append(request)
        assert dict(request.url.params) == {"path": "/output/fictional.docx"}
        assert "range" not in request.headers
        return httpx.Response(
            200, content=raw, headers={"content-type": "application/octet-stream"}
        )

    adapter = FilesystemAdapter(
        ConnectionConfig(domain="example.test", protocol="http"),
        SandboxEndpoint(endpoint="execd.example.test:44772", headers={}),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:

        async def http_client():
            return client

        monkeypatch.setattr(adapter, "_get_httpx_client", http_client)
        actual = await download_binary(
            SimpleNamespace(files=adapter), "/output/fictional.docx", len(raw)
        )
        assert actual == raw and digest(actual) == digest(raw)
        with pytest.raises(RuntimeFault, match="byte_budget"):
            await download_binary(
                SimpleNamespace(files=adapter), "/output/fictional.docx", len(raw) - 1
            )
    assert len(requests) == 2


async def test_stream_closed_on_budget_and_interruption():
    state = []

    class Files:
        async def read_bytes_stream(self, path):
            async def chunks():
                try:
                    yield b"first"
                    yield b"second"
                finally:
                    state.append("closed")

            return chunks()

    with pytest.raises(RuntimeFault, match="byte_budget"):
        await download_binary(SimpleNamespace(files=Files()), "/output/x", 1)
    assert state == ["closed"]


async def test_openrouter_dedicated_key_profile_no_fallback(monkeypatch):
    payloads = []

    def serve(request):
        import json

        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer fictional-router-key"
        payload = json.loads(request.content)
        payloads.append(payload)
        assert payload["model"] == MODEL
        assert payload["provider"] == {"allow_fallbacks": False, "require_parameters": True}
        return httpx.Response(
            200,
            json={
                "id": "fictional-response",
                "model": MODEL,
                "object": "chat.completion",
                "created": 0,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "ok"},
                    }
                ],
            },
        )

    monkeypatch.setenv("DEEPSEEK_API_KEY", "fictional-wrong-key")
    client, model = openrouter_model(
        "fictional-router-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(serve))
    )
    try:
        assert client.max_retries == 0
        await Agent(
            model,
            model_settings={
                "extra_body": {"provider": {"allow_fallbacks": False, "require_parameters": True}}
            },
        ).run("Fictional request")
    finally:
        await client.close()
    assert len(payloads) == 1
    with pytest.raises(RuntimeFault, match="openrouter_api_key_required"):
        openrouter_model("")
    with pytest.raises(RuntimeFault, match="openrouter_api_key_required"):
        PydanticHarness(openrouter_api_key="")


async def test_native_pydantic_checkpoint_new_gateway(case, monkeypatch):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from foliorun.adapters import pydantic_ai as adapter
    from foliorun.contracts import BlobRef, JobSpec
    from foliorun.tools import Gateway

    def respond(messages, info):
        if messages[-1].parts[0].part_kind == "user-prompt":
            return ModelResponse(
                parts=[ToolCallPart("submit_result", {"files": ["/output/x"]}, "fresh-submit")]
            )
        return ModelResponse(parts=[TextPart("Submitted")])

    model = FunctionModel(respond)

    class Client:
        async def close(self):
            pass

    async def country():
        return "US"

    monkeypatch.setattr(adapter, "country_only", country)
    monkeypatch.setattr(adapter, "openrouter_model", lambda key: (Client(), model))
    c = case
    c.scheduler.start = no_start
    h = await c.runtime.submit(
        prompt="Fictional", vfs=c.vfs, tools=c.tools, idempotency_key="native"
    )
    job = JobSpec.model_validate(
        c.store.read_json(
            BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
        )
    )
    from conftest import FakeBox

    first = Gateway(c.store, h.run_id, job, c.profile, c.tools, FakeBox(), {})
    c.executor.update_run(h.run_id, sandbox_id=first.box.id, retain_until=first.box.retain_until)
    harness = PydanticHarness(openrouter_api_key="fictional-only")
    checkpoint = await harness.run("Fictional first round", first, None)
    assert first.submitted
    restored = harness.restore(checkpoint)
    assert any(p.part_kind == "tool-call" for m in restored for p in m.parts)
    assert b"fictional-only" not in checkpoint


async def no_start(*args):
    pass


async def test_model_response_save_failure_leaves_intent_no_second_paid_call(case):
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.function import FunctionModel

    from foliorun.adapters.pydantic_ai import JournalModel
    from foliorun.contracts import BlobRef, JobSpec
    from foliorun.tools import Gateway

    c = case
    c.scheduler.start = no_start
    h = await c.runtime.submit(
        prompt="Fictional fault", vfs=c.vfs, tools=c.tools, idempotency_key="model-fault"
    )
    job = JobSpec.model_validate(
        c.store.read_json(
            BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
        )
    )
    gw = Gateway(c.store, h.run_id, job, c.profile, c.tools, None, {})
    calls = []

    def respond(messages, info):
        calls.append("paid-send")
        return ModelResponse(parts=[TextPart("Fictional response")])

    model = FunctionModel(respond)
    original_put = c.store.put

    def fail(data):
        raise OSError("injected durable response failure")

    c.store.put = fail
    messages = [ModelRequest(parts=[UserPromptPart("Fictional request")])]
    with pytest.raises(OSError):
        await JournalModel(model, gw).request(messages, None, ModelRequestParameters())
    c.store.put = original_put
    with pytest.raises(RuntimeFault, match="unknown_model_window"):
        await JournalModel(model, gw).request(messages, None, ModelRequestParameters())
    assert calls == ["paid-send"]
