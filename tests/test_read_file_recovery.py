import asyncio
import multiprocessing
import os

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from foliorun import (
    LocalArtifactStore,
    MemoryFS,
    Runtime,
    RuntimeConfig,
    RuntimeFault,
    SandboxCommand,
    ToolRegistry,
)
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.contracts import Snapshot, digest
from foliorun.tools import Gateway
from foliorun.vfs import LazyFiles

SOURCE = "/documents/fictional-private.bin"
OUTPUT = "/output/fictional-result.txt"
PAYLOAD = b"fictional-private-payload"


def journal_key(run_id, call_id):
    return f"journal/{run_id}/" + digest(f"tool:{call_id}".encode())


@pytest.mark.parametrize(("body", "code"), [
    (PAYLOAD + b"\xff", "file_not_utf8"),
    (PAYLOAD + b"\x00", "file_not_text"),
])
async def test_native_model_can_choose_another_tool_and_submit(case, body, code):
    expected = {"kind": "json", "value": {"error": {"code": code}}}
    registry = ToolRegistry(release="fictional-recovery")
    spec, _ = case.tools.entries["lookup_record"]
    registry.register(spec.model_copy(update={
        "name": "convert_fictional", "execution_location": "sandbox", "effect": "workspace_write",
        "input_schema": {
            "type": "object", "properties": {"source": {"const": SOURCE}},
            "required": ["source"], "additionalProperties": False,
        },
        "input_paths": ("source",),
    }), SandboxCommand(entrypoint="fictional-converter"))
    case.bindings.install_tools(registry)
    profile = case.profile.model_copy(update={"allowed_tools": ("convert_fictional",)})
    original_create = case.provider.create
    conversions = []

    async def create(*args):
        box = await original_create(*args)

        async def convert(mapping, args, call_id, _limits):
            assert mapping.entrypoint == "fictional-converter"
            assert args == {"source": SOURCE} and box.received[SOURCE] == body
            conversions.append(call_id)
            box.outputs[OUTPUT] = b"Fictional conversion"
            return {"kind": "json", "value": {"converted": True}}

        box.execute = convert
        return box

    case.provider.create = create
    receipts = []

    def respond(messages, _info):
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart):
                receipts.append((part.tool_call_id, part.content))
        if not receipts:
            return ModelResponse([ToolCallPart("read_file", {"path": SOURCE}, tool_call_id="read")])
        if receipts[-1] == ("read", expected):
            row = case.store.get_record(journal_key(run_id, "read"))[1]
            assert row["state"] == "done" and row["result"] == expected
            return ModelResponse([
                ToolCallPart("convert_fictional", {"source": SOURCE}, tool_call_id="convert"),
            ])
        if receipts[-1][0] == "convert":
            return ModelResponse([
                ToolCallPart("submit_result", {"files": [OUTPUT]}, tool_call_id="submit"),
            ])
        assert receipts[-1] == ("submit", {"kind": "json", "value": {"accepted": True, "files": 1}})
        return ModelResponse([TextPart("Submitted")])

    model = FunctionModel(respond, model_name=profile.model)
    case.executor.harness = PydanticHarness(model_factory=lambda: model)
    runtime = Runtime(config=RuntimeConfig(profile, case.scheduler), bindings=case.bindings,
                      store=case.store)
    handle = await runtime.submit(
        prompt="Convert a fictional file and submit.",
        vfs=MemoryFS({SOURCE: body}), tools=registry, idempotency_key="fictional-recovery",
        session_id="fictional-recovery", session_revision=0,
    )
    run_id = handle.run_id
    result = await runtime.wait(handle)
    assert result.status == "SUCCEEDED", result.error
    assert result.error is None and result.error_detail is None
    assert result.session_version.revision == 1
    assert receipts[0] == ("read", expected)
    assert [call_id for call_id, _ in receipts] == ["read", "convert", "submit"]
    assert conversions == ["convert"]
    assert case.store.get_record(journal_key(run_id, "read"))[1]["state"] == "done"
    assert len(result.artifacts) == 1 and result.artifacts[0].path == OUTPUT
    assert case.store.read(result.artifacts[0].blob) == b"Fictional conversion"


@pytest.mark.parametrize(("scenario", "status", "code"), [
    ("previous", "FAILED", "file_not_utf8"),
    ("storage-code", "FAILED", "file_not_utf8"),
    ("storage-io", "NEEDS_REVIEW", "adapter_failed"),
    ("float-offset", "FAILED", "tool_arguments_invalid"),
    ("float-length", "FAILED", "tool_arguments_invalid"),
    ("result-budget", "FAILED", "tool_result_invalid"),
    ("page-budget", "FAILED", "file_page_budget"),
    ("tool-budget", "FAILED", "tool_budget"),
])
async def test_other_failures_are_not_recoverable_tool_results(case, scenario, status, code):
    limits = case.profile.limits.model_copy(update={"tools": 1, "text_bytes": 50})
    if scenario not in {"result-budget", "page-budget"}:
        limits = case.profile.limits.model_copy(update={"tools": 1})
    profile = case.profile.model_copy(update={"limits": limits})
    runtime = Runtime(config=RuntimeConfig(profile, case.scheduler), bindings=case.bindings,
                      store=case.store)

    async def harness(_prompt, gateway, _checkpoint):
        args = {"path": SOURCE}
        if scenario == "previous":
            args["path"] = "/previous/fictional.bin"
            gateway.files[args["path"]] = gateway.files[SOURCE]
        elif scenario in {"storage-code", "storage-io"}:
            async def broken_read(_path):
                if scenario == "storage-code":
                    raise RuntimeFault("file_not_utf8")
                raise OSError("fictional I/O exception details")

            gateway.lazy.read = broken_read
        elif scenario in {"float-offset", "float-length"}:
            args[scenario.removeprefix("float-")] = 1.0
        elif scenario == "tool-budget":
            result = await gateway.invoke("read_file", args, "first-read")
            assert result == {"kind": "json", "value": {"error": {"code": "file_not_utf8"}}}
        await gateway.invoke("read_file", args, "rejected")
        pytest.fail("A fatal tool failure returned to the harness")

    case.harness.run = harness
    body = b"fictional text" if scenario == "page-budget" else b"\xff"
    result = await runtime.run(
        prompt="Read a fictional input.", vfs=MemoryFS({SOURCE: body}), tools=case.tools,
        idempotency_key="fictional-failure",
    )
    assert result.status == status and result.error == code
    assert not result.artifacts and result.session_version is None
    assert "fictional I/O exception details" not in result.model_dump_json()
    row = case.store.get_record(journal_key(result.handle.run_id, "rejected"))
    if scenario.startswith("float-") or scenario == "tool-budget":
        assert row is None
    else:
        assert row[1]["state"] == "intent" and "result" not in row[1]


@pytest.mark.parametrize("committed", [False, True], ids=["before-done", "after-done"])
async def test_hard_exit_does_not_reexecute_tool_calls(case, tmp_path, committed):
    effect = tmp_path / "fictional-effect"
    registry = ToolRegistry(release="fictional-effect")
    spec, _ = case.tools.entries["lookup_record"]

    async def record_effect(_args, _ctx):
        with effect.open("ab") as stream:
            stream.write(b"sent\n")
        return {"kind": "json", "value": {"sent": True}}

    async def no_start(*_args):
        pass

    registry.register(spec.model_copy(update={"effect": "external_side_effect"}), record_effect)
    case.bindings.install_tools(registry)
    case.scheduler.start = no_start
    store_root = tmp_path / "store"
    handle = await case.runtime.submit(
        prompt="Fictional persistence boundary.", vfs=MemoryFS({SOURCE: b"\xff"}),
        tools=registry, idempotency_key="fictional-crash",
    )
    key = journal_key(handle.run_id, "read")
    job = case.executor.job(case.store.get_record(f"run/{handle.run_id}")[1])
    snapshot = Snapshot.model_validate(case.store.read_json(job.effective_input_ref))

    def new_gateway():
        store = LocalArtifactStore(store_root, case.store.scope)
        files = {file.path: file for file in snapshot.files}
        lazy = LazyFiles(store, case.bindings, files, job.limits)
        return Gateway(store, handle.run_id, job, case.profile, registry, None, files, lazy=lazy)

    async def interrupt():
        gateway = new_gateway()
        complete = gateway.journal.complete

        def exit_at_completion(record_key, result):
            if record_key == key:
                if committed:
                    complete(record_key, result)
                os._exit(73)
            complete(record_key, result)

        gateway.journal.complete = exit_at_completion
        await gateway.invoke("lookup_record", {"id": "fictional"}, "effect")
        await gateway.invoke("read_file", {"path": SOURCE}, "read")

    process = multiprocessing.get_context("fork").Process(target=lambda: asyncio.run(interrupt()))
    process.start()
    try:
        process.join(timeout=10)
        assert process.exitcode == 73
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        process.close()

    gateway = new_gateway()
    row = gateway.store.get_record(key)
    ref = snapshot.files[0].blob
    (gateway.store.blob_store.blobs / ref.sha256).write_bytes(b"!")
    if committed:
        expected = {"kind": "json", "value": {"error": {"code": "file_not_utf8"}}}
        assert row[1]["state"] == "done" and row[1]["result"] == expected
        assert await gateway.invoke("read_file", {"path": SOURCE}, "read") == expected
        with pytest.raises(RuntimeFault, match="call_id_conflict"):
            await gateway.invoke("read_file", {"path": SOURCE, "offset": 1}, "read")
    else:
        assert row[1]["state"] == "intent"
        with pytest.raises(RuntimeFault, match="unknown_tool_window"):
            await gateway.invoke("read_file", {"path": SOURCE}, "read")
    assert gateway.store.get_record(key) == row
    assert await gateway.invoke("lookup_record", {"id": "fictional"}, "effect") == {
        "kind": "json", "value": {"sent": True},
    }
    assert effect.read_bytes() == b"sent\n"
