"""Installed vendor clients, controlled transport. These do NOT claim real Server/model success."""

import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI
from opensandbox.adapters.filesystem_adapter import FilesystemAdapter
from opensandbox.config import ConnectionConfig
from opensandbox.models.sandboxes import SandboxEndpoint
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from foliorun import Profile, RuntimeConfig, RuntimeFault
from foliorun.adapters.opensandbox import download_binary
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.contracts import digest
from foliorun.tools import Journal


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


@pytest.mark.parametrize("route,use_factory", [("cedar", False), ("birch", True)])
async def test_native_model_transports_run_tools_and_submit(
    case, route, use_factory, monkeypatch, caplog
):
    caplog.set_level("INFO")
    requests, identities = [], []
    key = f"fictional-{route}-host-key"
    model_name = f"fictional-{route}-model"
    header = f"fictional-{route}-private-header"
    endpoint = f"https://{route}.example.test/v1"
    case.profile = Profile.model_validate(case.profile.model_dump() | {"model": model_name})
    case.bindings.install_profile(case.profile)
    case.runtime.config = RuntimeConfig(case.profile, case.scheduler)
    begin = Journal.begin

    def trace_identity(self, kind, call_id, identity):
        identities.append(identity)
        return begin(self, kind, call_id, identity)

    monkeypatch.setattr(Journal, "begin", trace_identity)

    def serve(request):
        assert str(request.url) == f"{endpoint}/chat/completions"
        assert request.headers["authorization"] == f"Bearer {key}"
        assert request.headers["x-fictional-secret"] == header
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == model_name
        assert key not in request.content.decode() and header not in request.content.decode()
        assert not {"provider", "max_tokens", "max_completion_tokens"}.intersection(payload)
        assert {"lookup_record", "submit_result"} <= {
            tool["function"]["name"] for tool in payload["tools"]
        }
        if len(requests) == 1:
            assert any(message["content"] == "Fictional request" for message in payload["messages"])
        if len(requests) == 2:
            assert json.loads(payload["messages"][-1]["content"])["value"] == {"id": route}
        calls = [
            ("lookup_record", {"id": route}),
            ("submit_result", {"files": ["/output/synthetic.bin"]}),
        ]
        message = {"role": "assistant", "content": "Submitted"}
        if len(requests) <= len(calls):
            name, arguments = calls[len(requests) - 1]
            message = {
                "role": "assistant",
                "tool_calls": [{
                    "id": f"fictional-call-{len(requests)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            }
        return httpx.Response(
            200,
            json={
                "id": "fictional-response",
                "model": model_name,
                "object": "chat.completion",
                "created": 0,
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls" if "tool_calls" in message else "stop",
                        "message": message,
                    }
                ],
            },
        )

    transport = httpx.AsyncClient(transport=httpx.MockTransport(serve), trust_env=False)
    client = AsyncOpenAI(api_key=key, base_url=endpoint, http_client=transport, max_retries=0)
    model = OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(openai_client=client),
        settings={"extra_headers": {"x-fictional-secret": header}},
    )
    try:
        case.executor.harness = (
            PydanticHarness(model_factory=lambda: (client, model))
            if use_factory else PydanticHarness(model=model)
        )
        result = await case.runtime.run(
            prompt="Fictional request", vfs=case.vfs, tools=case.tools, idempotency_key=route
        )
        assert client.is_closed() is use_factory
    finally:
        await client.close()
    assert len(requests) == 3
    assert result.status == "SUCCEEDED", result.error
    assert result.session_version.revision == 1
    assert case.store.read(result.artifacts[0].blob) == b"entirely fictional binary\x00\xff\n"
    persisted = [path.read_bytes() for path in case.store.root.rglob("*") if path.is_file()]
    persisted.extend((case.profile.model_dump_json().encode(), json.dumps(identities).encode()))
    assert all(key.encode() not in body and header.encode() not in body for body in persisted)
    assert key not in caplog.text and header not in caplog.text


async def test_missing_bad_and_mismatched_model_injection(case, monkeypatch, caplog):
    from pydantic_ai.models.function import FunctionModel

    marker = "fictional-unused-credential"
    monkeypatch.setenv("OPENAI_API_KEY", marker)
    with pytest.raises(RuntimeFault, match="^model_injection_required$"):
        PydanticHarness()
    with pytest.raises(RuntimeFault, match="^invalid_model_binding$"):
        PydanticHarness(model="fictional-name")

    def unexpected_send(messages, info):
        pytest.fail("A mismatched model must not receive requests")

    def failed_factory():
        raise RuntimeFault(marker)

    for factory, expected in (
        (lambda: FunctionModel(unexpected_send, model_name="wrong-fictional"),
         "model_binding_mismatch"),
        (failed_factory, "model_setup_failed"),
        (lambda: None, "invalid_model_binding"),
    ):
        case.executor.harness = PydanticHarness(model_factory=factory)
        result = await case.runtime.run(
            prompt="Fictional request", vfs=case.vfs, tools=case.tools, idempotency_key=expected
        )
        assert result.error == expected
        assert marker not in result.model_dump_json()
    assert marker not in caplog.text


async def test_native_pydantic_checkpoint_new_gateway(case):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from foliorun.contracts import BlobRef, JobSpec
    from foliorun.tools import Gateway

    def respond(messages, info):
        if messages[-1].parts[0].part_kind == "user-prompt":
            return ModelResponse(
                parts=[ToolCallPart("submit_result", {"files": ["/output/x"]}, "fresh-submit")]
            )
        return ModelResponse(parts=[TextPart("Submitted")])

    c = case
    model = FunctionModel(respond, model_name=c.profile.model)
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
    harness = PydanticHarness(model_factory=lambda: model)
    checkpoint = await harness.run("Fictional first round", first, None)
    assert first.submitted
    restored = harness.restore(checkpoint)
    assert any(p.part_kind == "tool-call" for m in restored for p in m.parts)


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
