"""Synthetic output routing and adversarial POSIX files; no recorded model data."""

import asyncio
import json
import os
import shlex
import sys
from importlib.resources import files
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from foliorun import Limits, RuntimeFault
from foliorun._sandbox_inspect import InspectionFault, inspect_output, text_page
from foliorun.adapters.opensandbox import OpenSandboxSession
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.contracts import canonical, digest
from foliorun.tools import Journal

PRIVATE = "fictional-private-sentinel"


def local_session(root):
    """Translate only the fixed sandbox root in a synthetic command transport."""
    calls = []

    async def command(text, opts):
        argv = shlex.split(text)
        assert argv[:7] == ["runuser", "-u", "app", "--", "python", "-I", "-c"]
        assert argv[7] == files("foliorun").joinpath("_sandbox_inspect.py").read_text()
        assert opts.working_directory == "/workspace"
        calls.append(json.loads(argv[8]))
        source = argv[7].replace('inspect_output("/output", **args)',
                                 f"inspect_output({str(root)!r}, **args)")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", source, argv[8],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        out, err = await proc.communicate()
        assert not err
        return SimpleNamespace(error=None, exit_code=proc.returncode, text=out.decode())

    sdk = SimpleNamespace(id="fictional-local-box", commands=SimpleNamespace(run=command))
    return OpenSandboxSession(sdk), calls


async def execute(case, key="inspect", **kwargs):
    return await case.runtime.run(
        prompt="Inspect entirely fictional files and submit.",
        vfs=kwargs.pop("vfs", case.vfs), tools=case.tools, idempotency_key=key, **kwargs,
    )


async def test_native_tools_produce_inspect_read_then_submit(case, tmp_path):
    root = tmp_path / "output"
    root.mkdir()
    session, calls = local_session(root)
    original_create = case.provider.create

    async def create(*args):
        box = await original_create(*args)
        box.inspect_file = session.inspect_file
        # A synthetic approved sandbox tool produces a run-local manifest.
        async def produce(mapping, args, call_id, limits):
            body = '{"fictional":true,"label":"文档"}'.encode()
            (root / "manifest.json").write_bytes(body)
            box.outputs["/output/manifest.json"] = body
            return {"kind": "json", "value": {"files": ["/output/manifest.json"]}}
        box.execute = produce
        return box

    case.provider.create = create
    from foliorun import RuntimeConfig, SandboxCommand, ToolRegistry, ToolSpec

    registry = ToolRegistry(release="fictional-output-producer")
    registry.register(ToolSpec(
        name="produce", description="Produce a fictional manifest.",
        execution_location="sandbox", effect="workspace_write",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    ), SandboxCommand(entrypoint="fictional"))
    case.tools = registry
    case.bindings.install_tools(registry)
    profile = case.profile.model_copy(update={"allowed_tools": ("produce",)})
    case.bindings.install_profile(profile)
    case.runtime.config = RuntimeConfig(profile, case.scheduler)
    steps = [
        ("produce", {}),
        ("stat_file", {"path": "/output/manifest.json"}),
        ("read_file", {"path": "/output/manifest.json", "length": 200}),
        ("read_file", {"path": "/documents/fictional.txt"}),
        ("submit_result", {"files": ["/output/manifest.json"]}),
    ]
    observed = []

    async def respond(messages, info):
        index = len(observed)
        if index:
            value = messages[-1].parts[0].content
            if index == 2:
                assert value["value"]["size"] == (root / "manifest.json").stat().st_size
            if index == 3:
                assert json.loads(value["value"]["text"])["fictional"] is True
            if index == 4:
                assert value["value"]["text"] == "Entirely fictional specimen"
        observed.append(index)
        if index == len(steps):
            return ModelResponse(parts=[TextPart("Submitted")])
        name, args = steps[index]
        return ModelResponse(parts=[ToolCallPart(name, args, f"fictional-{index}")])

    class Client:
        async def close(self):
            pass

    case.executor.harness = PydanticHarness(
        model_factory=lambda: (Client(), FunctionModel(respond))
    )
    result = await execute(case)
    assert result.status == "SUCCEEDED", result.error
    assert result.error_detail is None and len(result.artifacts) == 1
    assert case.store.read(result.artifacts[0].blob) == (root / "manifest.json").read_bytes()
    assert len(observed) == 6 and len(calls) == 2
    assert case.provider.boxes[0].destroyed


@pytest.mark.parametrize(("path", "code"), [
    ("/output/../inputs/private", "invalid_path"),
    ("/output//x", "invalid_path"),
    ("/output/./x", "invalid_path"),
    ("/output/\\x", "invalid_path"),
    ("/output/x\x00", "invalid_path"),
    ("relative", "invalid_path"),
    ("/etc/passwd", "reserved_input_path"),
    ("/workspace/private", "reserved_input_path"),
    ("/other-run/output/x", "reserved_input_path"),
    ("/documents/missing", "input_missing"),
    ("/previous/missing", "input_missing"),
])
async def test_gateway_denials_never_touch_sandbox(case, path, code, caplog):
    async def inspect(prompt, gateway, checkpoint):
        await gateway.invoke("read_file", {"path": path}, "deny")
    case.harness.run = inspect
    result = await execute(case)
    assert result.status == "FAILED" and result.error == code
    assert result.error_detail is None and not result.artifacts
    assert PRIVATE not in result.model_dump_json() + caplog.text
    assert path not in result.model_dump_json()
    assert case.executor.reserve_fallback(result.handle.run_id) is None


@pytest.mark.parametrize(("kind", "code"), [
    ("missing", "output_missing"), ("leaf_link", "output_unsafe_file"),
    ("parent_link", "output_unsafe_file"), ("root_link", "output_unsafe_file"),
    ("hardlink", "output_unsafe_file"), ("directory", "output_unsafe_file"),
    ("fifo", "output_unsafe_file"), ("large", "file_too_large"),
    ("binary", "file_not_utf8"), ("nul", "file_not_text"),
    ("del", "file_not_text"), ("c1", "file_not_text"),
])
async def test_real_helper_rejects_unsafe_files_safely(case, tmp_path, kind, code, caplog):
    root = tmp_path / "output"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_text(PRIVATE)
    target = "/output/x"
    if kind == "leaf_link":
        (root / "x").symlink_to(outside / "private")
    elif kind == "parent_link":
        (root / "link").symlink_to(outside, target_is_directory=True)
        target = "/output/link/private"
    elif kind == "root_link":
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
        target = "/output/private"
    elif kind == "hardlink":
        os.link(outside / "private", root / "x")
    elif kind == "directory":
        (root / "x").mkdir()
    elif kind == "fifo":
        os.mkfifo(root / "x")
    elif kind in {"large", "binary", "nul", "del", "c1"}:
        (root / "x").write_bytes({"large": b"x" * (case.profile.limits.output_bytes + 1),
                                   "binary": b"\xff", "nul": b"hello\x00",
                                   "del": b"\x7f", "c1": "\u0085".encode()}[kind])
    session, _ = local_session(root)

    async def inspect(prompt, gateway, checkpoint):
        gateway.box.inspect_file = session.inspect_file
        await gateway.invoke("read_file", {"path": target}, "deny")
    case.harness.run = inspect
    result = await execute(case)
    assert result.status == "FAILED" and result.error == code
    assert result.error_detail is None and not result.artifacts
    assert PRIVATE not in result.model_dump_json() + caplog.text
    assert str(tmp_path) not in result.model_dump_json()


async def test_other_session_has_no_access_to_first_output(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "x").write_text(PRIVATE)
    box, _ = local_session(second)
    with pytest.raises(RuntimeFault) as raised:
        await box.inspect_file("/output/x", Limits(), read=True)
    assert raised.value.code == "output_missing"
    assert PRIVATE not in str(raised.value)


@pytest.mark.parametrize("body", [b"", "文档\n\t\"\\".encode() * 15])
async def test_unicode_paging_respects_json_byte_budget(tmp_path, body):
    root = tmp_path / "output"
    root.mkdir()
    (root / "x").write_bytes(body)
    session, _ = local_session(root)
    limits = Limits(text_bytes=100, output_bytes=max(1, len(body)))
    offset, restored = 0, ""
    while True:
        page = await session.inspect_file("/output/x", limits, read=True,
                                          offset=offset, length=100)
        assert len(canonical(page)) <= limits.text_bytes
        restored += page["value"]["text"]
        offset = page["value"]["next_offset"]
        if page["value"]["eof"]:
            break
    assert restored.encode() == body
    metadata = await session.inspect_file("/output/x", Limits(output_bytes=max(1, len(body))))
    assert metadata["value"] == {"path": "/output/x", "size": len(body), "sha256": digest(body)}
    page = await session.inspect_file("/output/x", Limits(), read=True, offset=100000)
    assert page["value"] == {"text": "", "next_offset": 100000, "eof": True}


async def test_frozen_metadata_read_and_previous_remain_host_routed(case):
    async def inspect(prompt, gateway, checkpoint):
        async def forbidden(*args, **kwargs):
            pytest.fail("Frozen inputs must not read the sandbox")
        gateway.box.inspect_file = forbidden
        # A prior committed file's storage ref is aliased under /previous by Executor.
        gateway.files["/previous/fictional.txt"] = gateway.files["/documents/fictional.txt"]
        for n, path in enumerate(["/documents/fictional.txt", "/previous/fictional.txt"]):
            info = await gateway.invoke("stat_file", {"path": path}, f"stat-{n}")
            assert info["value"]["sha256"] == digest(b"Entirely fictional specimen")
            value = await gateway.invoke("read_file", {"path": path}, f"read-{n}")
            assert value["value"]["text"] == "Entirely fictional specimen"
        await gateway.invoke("submit_result", {"files": ["/output/x"]}, "submit")
        return b"[]"
    case.harness.run = inspect
    assert (await execute(case)).status == "SUCCEEDED"


@pytest.mark.parametrize("malformed", [
    {"error": PRIVATE}, {"error": [PRIVATE]}, {"kind": "json", "value": PRIVATE},
    {"kind": "json", "value": {"text": "", "next_offset": True, "eof": False}},
])
async def test_untrusted_receipt_cannot_inject_error_or_result(malformed):
    async def command(*args, **kwargs):
        return json.dumps(malformed)
    box = OpenSandboxSession(SimpleNamespace(id="fictional"))
    box.command = command
    with pytest.raises(RuntimeFault) as raised:
        await box.inspect_file("/output/x", Limits(), read=True)
    assert raised.value.code == "file_inspection_invalid"
    assert PRIVATE not in str(raised.value)


async def test_transport_failure_is_unclassified_read_failure_without_exception_context():
    async def command(*args, **kwargs):
        raise OSError(PRIVATE)
    box = OpenSandboxSession(SimpleNamespace(id="fictional"))
    box.command = command
    with pytest.raises(RuntimeFault) as raised:
        await box.inspect_file("/output/x", Limits(), read=True)
    assert raised.value.code == "file_inspection_failed" and raised.value.detail is None
    assert raised.value.__context__ is None and raised.value.__cause__ is None
    assert PRIVATE not in repr(raised.value)


async def test_cancellation_during_inspection_prevents_submission(case):
    async def inspect(prompt, gateway, checkpoint):
        async def read(*args, **kwargs):
            case.executor.cancel_fence(gateway.run_id)
            return {"kind": "json", "value": {"text": "fictional", "next_offset": 9, "eof": True}}
        gateway.box.inspect_file = read
        await gateway.invoke("read_file", {"path": "/output/x"}, "read")
        pytest.fail("Cancellation must stop tool dispatch")
    case.harness.run = inspect
    result = await execute(case)
    assert result.status == "CANCELLED" and not result.artifacts


async def test_unknown_tool_window_still_requires_review(case):
    async def inspect(prompt, gateway, checkpoint):
        identity = {"fictional": True}
        journal = Journal(case.store, gateway.run_id)
        journal.begin("tool", "pending", identity)
        journal.begin("tool", "pending", identity)
    case.harness.run = inspect
    result = await execute(case)
    assert result.status == "NEEDS_REVIEW" and result.error == "unknown_tool_window"
    assert case.executor.reserve_fallback(result.handle.run_id) is None


def test_too_small_page_budget_and_mutation_detection(tmp_path, monkeypatch):
    with pytest.raises(InspectionFault, match="file_page_budget"):
        text_page(b"fictional", 0, 100, 1)
    root = tmp_path / "output"
    root.mkdir()
    target = root / "x"
    target.write_bytes(b"fictional")
    original = os.fstat
    count = 0
    def changing(fd):
        nonlocal count
        count += 1
        result = original(fd)
        if count == 1:
            target.write_bytes(b"altered")
        return result
    monkeypatch.setattr(os, "fstat", changing)
    with pytest.raises(InspectionFault, match="output_changed"):
        inspect_output(str(root), "/output/x", 100)


@pytest.mark.parametrize("args", [
    {"path": "/output/x", "offset": -1}, {"path": "/output/x", "offset": True},
    {"path": "/output/x", "length": 0}, {"path": "/output/x", "length": 131073},
    {"path": "/output/x", "run_id": "fictional-other-run"}, {"path": 1},
])
async def test_closed_schema_rejects_invalid_inspection_arguments(case, args):
    async def inspect(prompt, gateway, checkpoint):
        await gateway.invoke("read_file", args, "bad-args")
    case.harness.run = inspect
    result = await execute(case)
    assert result.status == "FAILED" and result.error == "tool_arguments_invalid"
    assert result.error_detail is None


async def test_custom_adapter_without_inspection_fails_closed(case):
    async def inspect(prompt, gateway, checkpoint):
        await gateway.invoke("stat_file", {"path": "/output/x"}, "missing-inspector")
    case.harness.run = inspect
    result = await execute(case)
    assert result.status == "FAILED" and result.error == "file_inspection_unavailable"
    assert result.error_detail is None


async def test_binary_stat_allowed_and_sealed_session_not_readable(tmp_path):
    (tmp_path / "x").write_bytes(b"\xff\x00")
    box, _ = local_session(tmp_path)
    result = await box.inspect_file("/output/x", Limits())
    assert result["value"]["size"] == 2
    box.sealed = True
    with pytest.raises(RuntimeFault, match="output_sealed"):
        await box.inspect_file("/output/x", Limits(), read=True)


@pytest.mark.parametrize("path", ["/output/../x", "/etc/passwd", "/output//x"])
def test_sandbox_revalidates_paths_without_gateway(tmp_path, path):
    with pytest.raises(InspectionFault, match="invalid_output_path"):
        inspect_output(str(tmp_path), path, 100)


def test_max_page_budget_leaves_room_for_command_newline():
    page = text_page(b"x" * 140000, 0, 131072, 131071)
    assert len(canonical(page)) + 1 <= 131072
    assert not page["value"]["eof"]
