"""Count semantics reused from the independent None-aware candidate, wire 0.3 defaults."""

import json

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel

from foliorun import Limits, Profile, RuntimeConfig, RuntimeFault
from foliorun.adapters import pydantic_ai as adapter
from foliorun.contracts import BlobRef, JobSpec
from foliorun.tools import Gateway


def configure(c, limit):
    data = c.profile.model_dump()
    data["limits"].update(model_requests=limit, tools=limit)
    c.profile = Profile.model_validate(data)
    c.bindings.install_profile(c.profile)
    c.runtime.config = RuntimeConfig(c.profile, c.scheduler)


def job_for(c, handle):
    state = c.store.get_record("run/" + handle.run_id)[1]
    return JobSpec.model_validate(c.store.read_json(BlobRef.model_validate(state["job_ref"])))


@pytest.mark.parametrize("field", ["model_requests", "tools"])
@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize("value", [0, -1, "4", True, False, 4.0, 4.5, [], {}])
def test_strict_count_rejection(field, mode, value):
    with pytest.raises(ValidationError):
        if mode == "python":
            Limits.model_validate({field: value})
        else:
            Limits.model_validate_json(json.dumps({field: value}))


@pytest.mark.parametrize("value", [None, 1, 17, 65, 180])
def test_counts_roundtrip_without_old_maximum(value):
    limits = Limits(model_requests=value, tools=value)
    assert Limits.model_validate_json(limits.model_dump_json()) == limits
    assert Limits().model_requests is None and Limits().tools is None
    for field in ("model_requests", "tools"):
        schema = Limits.model_json_schema()["properties"][field]
        assert schema["default"] is None
        assert {"type": "null"} in schema["anyOf"]


@pytest.mark.parametrize("limit", [None, 180])
async def test_actual_agent_105_requests_104_tools_explicit_sdk_none(case, monkeypatch, limit):
    c = case
    configure(c, limit)
    calls, sdk_kwargs = [], []

    def respond(messages, info):
        calls.append(len(messages))
        index = len(calls)
        if index <= 103:
            return ModelResponse(parts=[ToolCallPart("list_files", {}, f"list-{index}")])
        if index == 104:
            return ModelResponse(
                parts=[
                    ToolCallPart("submit_result", {"files": ["/output/synthetic.bin"]}, "submit")
                ]
            )
        return ModelResponse(parts=[TextPart("Submitted")])

    original = adapter.UsageLimits

    def capture(**kwargs):
        sdk_kwargs.append(kwargs)
        return original(**kwargs)

    class Client:
        async def close(self):
            pass

    monkeypatch.setattr(adapter, "UsageLimits", capture)
    c.executor.harness = adapter.PydanticHarness(
        model_factory=lambda: (Client(), FunctionModel(respond, model_name=c.profile.model))
    )
    handle = await c.runtime.submit(
        prompt="Entirely fictional counts", vfs=c.vfs, tools=c.tools, idempotency_key="counts"
    )
    result = await c.runtime.wait(handle, timeout=30)
    assert result.status == "SUCCEEDED", result.error
    assert len(calls) == 105
    assert sdk_kwargs == [{"request_limit": limit, "tool_calls_limit": limit}]
    assert job_for(c, handle).limits.model_requests == limit
    rows = c.store.repository.scan("journal/" + handle.run_id + "/")
    assert len(rows) == 209


@pytest.mark.parametrize("limit", [2, 101])
async def test_explicit_finite_counts_refuse_before_send(case, limit):
    c = case
    configure(c, limit)

    async def no_start(*args):
        pass

    c.scheduler.start = no_start
    handle = await c.runtime.submit(
        prompt="Fictional finite", vfs=c.vfs, tools=c.tools, idempotency_key="finite"
    )
    gateway = Gateway(c.store, handle.run_id, job_for(c, handle), c.profile, c.tools, None, {})
    sends = []

    def respond(messages, info):
        sends.append(1)
        return ModelResponse(parts=[TextPart("Synthetic response")])

    wrapped = adapter.JournalModel(FunctionModel(respond), gateway)
    messages = [ModelRequest(parts=[UserPromptPart("Fictional")])]
    for index in range(limit):
        await wrapped.request(messages, None, ModelRequestParameters())
        await gateway.invoke("list_files", {}, f"tool-{index}")
    with pytest.raises(RuntimeFault, match="^model_budget$"):
        await wrapped.request(messages, None, ModelRequestParameters())
    with pytest.raises(RuntimeFault, match="^tool_budget$"):
        await gateway.invoke("list_files", {}, "excess")
    assert len(sends) == limit
