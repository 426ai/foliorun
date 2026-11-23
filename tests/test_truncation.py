"""Synthetic SDK/wire regressions. No provider recordings or real credentials."""

import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.exceptions import (
    ContentFilterError,
    IncompleteToolCall,
    ModelConnectionError,
    ModelHTTPError,
    ModelTimeoutError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from foliorun import (
    FallbackPolicy,
    Handle,
    MemoryFS,
    RuntimeConfig,
    RuntimeFault,
    SafeError,
    ToolRegistry,
)
from foliorun.adapters.pydantic_ai import (
    JournalModel,
    PydanticHarness,
    safe_model_error,
)
from foliorun.contracts import BlobRef, JobSpec, canonical
from foliorun.tools import Gateway

PRIVATE = "synthetic-private-marker"
MODEL = "fictional-model"


def completion(*, finish_reason="length", reasoning=True):
    message = {"role": "assistant", "content": None}
    if reasoning:
        message["reasoning"] = "Entirely fictional thinking."
    return {
        "id": "synthetic-response",
        "model": MODEL,
        "object": "chat.completion",
        "created": 0,
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 1800,
            "total_tokens": 1810,
            "completion_tokens_details": {"reasoning_tokens": 1800},
        },
    }


def model_factory(serve, *, settings=None, profile=None):
    def create():
        client = AsyncOpenAI(
            api_key="fictional-only",
            base_url="https://model.example.test/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(serve)),
        )
        model = OpenAIChatModel(
            MODEL, provider=OpenAIProvider(openai_client=client), profile=profile, settings=settings
        )
        return client, model

    return create


@pytest.mark.parametrize("reasoning", [False, True])
async def test_sdk_baseline_length_has_no_actionable_output(reasoning):
    client, model = model_factory(
        lambda request: httpx.Response(200, json=completion(reasoning=reasoning))
    )()
    try:
        with pytest.raises(UnexpectedModelBehavior) as raised:
            await Agent(model, retries=0, model_settings={"max_tokens": 1800}).run("Fictional")
        # Assert the type, not potentially sensitive SDK messages/bodies.
        assert type(raised.value) is UnexpectedModelBehavior
    finally:
        await client.close()


@pytest.mark.parametrize("reasoning", [False, True])
async def test_framework_classifies_truncation(case, reasoning):
    payloads = []

    def serve(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=completion(reasoning=reasoning))

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Entirely fictional truncation",
        vfs=case.vfs,
        tools=case.tools,
        idempotency_key="truncation",
    )
    assert result.status == "FAILED"
    assert result.error == "model_output_truncated"
    assert result.error_detail.cause == "output_truncated"
    assert result.error_detail.finish_reason == "length"
    assert result.error_detail.truncated is True
    assert result.error_detail.model_request_index == 1
    assert not result.artifacts and not result.session_version
    assert len(payloads) == 1
    assert "max_tokens" not in payloads[0] and "max_completion_tokens" not in payloads[0]
    state = case.store.get_record("run/" + result.handle.run_id)[1]
    assert not case.runtime.session_versions(case.executor.job(state).session_id)


async def test_bounded_four_response_synthetic_skill_read_reproduction(case):
    case.vfs = MemoryFS({"/skills/fictional/SKILL.md": b"Entirely fictional Skill."})
    sends = []

    def serve(request):
        sends.append(1)
        if len(sends) <= 3:
            response = tool_completion(
                "read_file", '{"path":"/skills/fictional/SKILL.md","offset":0,"length":8}'
            )
            response["choices"][0]["message"]["tool_calls"][0]["id"] = f"read-{len(sends)}"
        else:
            assert len(sends) == 4
            response = completion()
        return httpx.Response(200, json=response)

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Entirely fictional four-response case",
        vfs=case.vfs,
        tools=case.tools,
        idempotency_key="four-responses",
    )
    assert sends == [1] * 4
    assert result.status == "FAILED" and result.error == "model_output_truncated"
    assert result.error_detail.model_request_index == 4
    assert result.error_detail.finish_reason == "length" and result.error_detail.truncated
    assert len(case.store.repository.scan("journal/" + result.handle.run_id + "/")) == 7
    assert not case.store.repository.scan("collection/")
    assert not result.artifacts and result.session_version is None


def tool_completion(name="submit_result", arguments=None, finish_reason="tool_calls"):
    response = completion(finish_reason=finish_reason, reasoning=False)
    response["choices"][0]["message"]["tool_calls"] = [
        {
            "id": "synthetic-call",
            "type": "function",
            "function": {
                "name": name,
                "arguments": arguments if arguments is not None else '{"files":["/output/x"]}',
            },
        }
    ]
    return response


@pytest.mark.parametrize("arguments", ['{"files":[', '{"files":["/output/x"]}'])
async def test_length_blocks_incomplete_and_even_parseable_tool_arguments(case, arguments):
    sends = []

    def serve(request):
        sends.append(1)
        return httpx.Response(
            200, json=tool_completion(arguments=arguments, finish_reason="length")
        )

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Fictional partial tool", vfs=case.vfs, tools=case.tools, idempotency_key="partial"
    )
    assert result.error == "model_output_truncated" and result.status == "FAILED"
    assert sends == [1]
    assert not case.provider.boxes[0].sealed
    assert case.store.repository.scan("collection/") == []
    assert not result.artifacts


async def test_normal_tool_calls_and_submission_still_succeed(case):
    sends = []

    def serve(request):
        sends.append(1)
        if len(sends) == 1:
            response = tool_completion("lookup_record", '{"id":"fictional"}')
        elif len(sends) == 2:
            response = tool_completion()
            response["choices"][0]["message"]["tool_calls"][0]["id"] = "synthetic-submit"
        else:
            response = completion(finish_reason="stop", reasoning=False)
            response["choices"][0]["message"]["content"] = "Submitted"
        return httpx.Response(200, json=response)

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Fictional success", vfs=case.vfs, tools=case.tools, idempotency_key="success"
    )
    assert result.status == "SUCCEEDED" and result.error is None
    assert result.error_detail is None
    assert result.session_version.revision == 1 and len(result.artifacts) == 1
    assert len(sends) == 3


@pytest.mark.parametrize("reason", ["stop", PRIVATE, None, "content_filter", "error"])
async def test_non_length_does_not_invent_truncation(case, reason, caplog):
    case.executor.harness = PydanticHarness(
        model_factory=model_factory(
            lambda request: httpx.Response(200, json=completion(finish_reason=reason))
        )
    )
    result = await case.runtime.run(
        prompt="Fictional no output", vfs=case.vfs, tools=case.tools, idempotency_key="no-output"
    )
    assert result.status == "FAILED" and not result.artifacts
    assert result.error in {"model_response_invalid", "model_content_filtered"}
    assert result.error_detail.truncated is not True
    assert PRIVATE not in result.model_dump_json() + caplog.text
    if reason in {PRIVATE, None}:
        assert result.error_detail.finish_reason is None


def test_safe_error_schema_rejects_raw_or_unbounded_fields():
    from pydantic import ValidationError

    for data in (
        {"message": PRIVATE},
        {"body": PRIVATE},
        {"cause": PRIVATE},
        {"finish_reason": PRIVATE},
        {"http_status": 999},
        {"model_request_index": PRIVATE},
        {"truncated": PRIVATE},
    ):
        with pytest.raises(ValidationError):
            SafeError.model_validate(data)


@pytest.mark.parametrize(
    "exc,cause",
    [
        (IncompleteToolCall(PRIVATE), "output_truncated"),
        (ContentFilterError(PRIVATE, body=PRIVATE), "content_filter"),
        (ModelHTTPError(429, MODEL, body={"error": PRIVATE}), "http_error"),
        (ModelTimeoutError(MODEL, PRIVATE), "timeout"),
        (ModelConnectionError(MODEL, PRIVATE), "transport_error"),
        (UnexpectedModelBehavior(PRIVATE, body=PRIVATE), "invalid_response"),
        (ValueError("length " + PRIVATE), "unknown"),
    ],
)
def test_only_allowlisted_types_become_causes(exc, cause):
    detail = safe_model_error(exc, 1)
    assert detail.cause == cause
    assert detail.finish_reason is None
    assert PRIVATE not in detail.model_dump_json()
    assert detail.truncated is (True if cause == "output_truncated" else None)


@pytest.mark.parametrize("failure", ["http", "timeout", "connection", "unknown", "invalid"])
async def test_request_failure_keeps_unknown_window_and_safe_evidence(case, failure, caplog):
    sends = []

    def serve(request):
        sends.append(1)
        if failure == "http":
            return httpx.Response(
                429,
                json={"error": {"message": PRIVATE, "metadata": {"raw": PRIVATE}}},
                headers={"x-request-id": PRIVATE},
            )
        if failure == "timeout":
            raise httpx.ReadTimeout(PRIVATE, request=request)
        if failure == "connection":
            raise httpx.ConnectError(PRIVATE, request=request)
        if failure == "invalid":
            return httpx.Response(200, json={"choices": [], "private": PRIVATE})
        raise ValueError(PRIVATE)

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Fictional request failure",
        vfs=case.vfs,
        tools=case.tools,
        idempotency_key="request-failure",
    )
    assert result.status == "NEEDS_REVIEW" and result.error == "unknown_model_window"
    expected = {
        "http": "http_error",
        "timeout": "timeout",
        "invalid": "invalid_response",
        "connection": "transport_error",
        "unknown": "unknown",
    }
    assert result.error_detail.cause == expected[failure]
    assert result.error_detail.truncated is None
    assert result.error_detail.finish_reason is None
    assert result.error_detail.http_status == (429 if failure == "http" else None)
    assert sends == [1]
    assert case.executor.reserve_fallback(result.handle.run_id) is None
    assert PRIVATE not in result.model_dump_json() + caplog.text
    rows = case.store.repository.scan("journal/" + result.handle.run_id + "/")
    assert len(rows) == 1 and rows[0][2]["state"] == "intent"
    assert PRIVATE not in canonical(rows).decode()


async def make_gateway(case):
    async def no_start(*args):
        pass

    case.scheduler.start = no_start
    handle = await case.runtime.submit(
        prompt="Fictional replay", vfs=case.vfs, tools=case.tools, idempotency_key="replay"
    )
    record = case.store.get_record("run/" + handle.run_id)[1]
    job = JobSpec.model_validate(case.store.read_json(BlobRef.model_validate(record["job_ref"])))
    return Gateway(case.store, handle.run_id, job, case.profile, case.tools, None, {})


async def test_cached_length_is_classified_without_resending_or_tool_dispatch(case):
    gateway = await make_gateway(case)
    sends = []

    def respond(messages, info):
        sends.append(1)
        return ModelResponse(parts=[], finish_reason="length")

    model = FunctionModel(respond)
    messages = [ModelRequest(parts=[UserPromptPart("Fictional")])]
    for _ in range(2):
        with pytest.raises(RuntimeFault) as raised:
            await JournalModel(model, gateway).request(
                messages,
                None,
                ModelRequestParameters(),
            )
        assert raised.value.code == "model_output_truncated"
        assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert sends == [1]


async def test_exception_object_and_its_sensitive_chain_are_not_retained(case):
    gateway = await make_gateway(case)

    def respond(messages, info):
        try:
            raise ValueError(PRIVATE)
        except ValueError as exc:
            raise UnexpectedModelBehavior(PRIVATE, body=PRIVATE) from exc

    with pytest.raises(RuntimeFault) as raised:
        await JournalModel(FunctionModel(respond), gateway).request(
            [ModelRequest(parts=[UserPromptPart("Fictional")])],
            None,
            ModelRequestParameters(),
        )
    fault = raised.value
    assert fault.code == "unknown_model_window"
    assert fault.detail.cause == "invalid_response"
    assert fault.__cause__ is None and fault.__context__ is None
    assert PRIVATE not in str(fault) + repr(fault) + fault.detail.model_dump_json()


@pytest.mark.parametrize("mode", ["fence", "interruption"])
async def test_cancellation_and_interruption_cannot_become_success(case, mode):
    sends = []
    handles = []

    def serve(request):
        sends.append(1)
        if mode == "fence":
            case.executor.cancel_fence(handles[0].run_id)
            return httpx.Response(200, json=completion())
        raise asyncio.CancelledError()

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    handle = await case.runtime.submit(
        prompt="Fictional cancellation", vfs=case.vfs, tools=case.tools, idempotency_key="cancel"
    )
    handles.append(handle)
    result = await case.runtime.wait(handle)
    assert result.status == ("CANCELLED" if mode == "fence" else "NEEDS_REVIEW")
    assert result.error == ("cancelled" if mode == "fence" else "unknown_execution_window")
    assert not result.artifacts and result.session_version is None
    assert case.executor.reserve_fallback(handle.run_id) is None
    assert sends == [1]


@pytest.mark.parametrize("typed", [True, False])
async def test_unknown_external_effect_is_never_retried_or_successful(case, typed):
    effects, sends = [], []
    registry = ToolRegistry("synthetic-external", release="synthetic-1")
    spec, _ = case.tools.entries["lookup_record"]

    async def effect(args, ctx):
        effects.append(1)
        if typed:
            raise RuntimeFault("unknown_tool_window")
        raise OSError(PRIVATE)

    registry.register(spec.model_copy(update={"effect": "external_side_effect"}), effect)
    case.tools = registry
    case.bindings.install_tools(registry)
    case.profile = case.profile.model_copy(update={"fallback": FallbackPolicy(max_attempts=1)})
    case.bindings.install_profile(case.profile)
    case.runtime.config = RuntimeConfig(case.profile, case.scheduler)

    def serve(request):
        sends.append(1)
        return httpx.Response(200, json=tool_completion("lookup_record", '{"id":"fictional"}'))

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Fictional unknown effect", vfs=case.vfs, tools=case.tools, idempotency_key="effect"
    )
    assert result.status == "NEEDS_REVIEW" and not result.artifacts
    assert result.error == ("unknown_tool_window" if typed else "adapter_failed")
    assert case.executor.reserve_fallback(result.handle.run_id) is None
    assert effects == sends == [1]
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.parametrize("reason", ["length", "stop"])
async def test_no_actionable_output_after_submit_does_not_publish_or_recover(case, reason):
    sends = []

    def serve(request):
        sends.append(1)
        return httpx.Response(
            200, json=tool_completion() if len(sends) == 1 else completion(finish_reason=reason)
        )

    case.executor.harness = PydanticHarness(model_factory=model_factory(serve))
    result = await case.runtime.run(
        prompt="Fictional post-submit truncation",
        vfs=case.vfs,
        tools=case.tools,
        idempotency_key="post-submit",
    )
    assert result.status == "FAILED"
    assert result.error == (
        "model_output_truncated" if reason == "length" else "model_response_invalid"
    )
    assert result.error_detail.model_request_index == 2
    assert not result.artifacts and result.session_version is None
    state = case.store.get_record("run/" + result.handle.run_id)[1]
    assert not state.get("agent_finished") and not state.get("native_ref")
    assert case.runtime.collection(result.handle)["phase"] != "complete"
    assert (
        await case.executor.recover_collection(
            result.handle.run_id, case.runtime.collection(result.handle)["seal_ref"]["sha256"]
        )
        == state["result"]
    )


async def test_close_failure_cannot_hide_classified_truncation(case):
    class Client:
        async def close(self):
            raise OSError(PRIVATE)

    case.executor.harness = PydanticHarness(
        model_factory=lambda: (
            Client(),
            FunctionModel(
                lambda messages, info: ModelResponse(
                    parts=[TextPart("Partial")], finish_reason="length"
                ),
                model_name=case.profile.model,
            ),
        )
    )
    result = await case.runtime.run(
        prompt="Fictional close failure", vfs=case.vfs, tools=case.tools, idempotency_key="close"
    )
    assert result.error == "model_output_truncated"
    assert result.error_detail.truncated is True
    assert PRIVATE not in result.model_dump_json()


async def test_temporal_activity_receipt_preserves_only_safe_failure_detail(case, monkeypatch):
    from foliorun.adapters.temporal import TemporalActivities, activity

    gateway = await make_gateway(case)
    state = case.store.get_record("run/" + gateway.run_id)[1]
    detail = SafeError(cause="output_truncated", finish_reason="length", truncated=True)

    async def fail(*args):
        raise RuntimeFault("model_output_truncated", detail=detail)

    monkeypatch.setattr(case.executor, "execute", fail)
    monkeypatch.setattr(activity, "heartbeat", lambda *args: None)
    assert (
        await TemporalActivities(case.executor).agent(
            {"run_id": gateway.run_id, "job_ref": state["job_ref"]}
        )
        == "receipt_persisted"
    )
    case.executor.finalize(gateway.run_id)
    result = await case.runtime.get(Handle.model_validate(state["handle"]))
    assert result.error == "model_output_truncated" and result.error_detail == detail


async def test_recovery_pending_poll_exposes_safe_evidence(case):
    gateway = await make_gateway(case)
    detail = SafeError()
    state = case.store.get_record("run/" + gateway.run_id)[1]
    case.executor.update_run(
        gateway.run_id,
        prepared={
            "status": "RECOVERY_PENDING",
            "error": "adapter_failed",
            "error_detail": detail.model_dump(),
            "collection_ref": state["job_ref"],
        },
    )
    result = await case.runtime.get(Handle.model_validate(state["handle"]))
    assert result.status == "RECOVERY_PENDING"
    assert result.error == "adapter_failed" and result.error_detail == detail
