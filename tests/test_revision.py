"""Synthetic regression for lazy inputs, complete revisions, collection and explicit attempts."""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest

from foliorun import (
    FallbackPolicy,
    LocalArtifactStore,
    MemoryFS,
    Profile,
    Runtime,
    RuntimeConfig,
    RuntimeFault,
    ToolRegistry,
    WorkerBindings,
)
from foliorun.collection import Collector
from foliorun.contracts import BlobRef, digest
from foliorun.execution import Executor
from foliorun.vfs import LazyFiles, freeze_vfs


class TrackingFS(MemoryFS):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    @asynccontextmanager
    async def open_read(self, revision, path):
        self.reads.append(path)
        async with super().open_read(revision, path) as chunks:
            yield chunks


async def run(c, key, *, vfs=None, revision=0, **kwargs):
    handle = await c.runtime.submit(
        prompt="Entirely fictional " + key,
        vfs=vfs if vfs is not None else c.vfs,
        tools=c.tools,
        idempotency_key=key,
        session_id="synthetic-session",
        session_revision=revision,
        **kwargs,
    )
    return handle, await c.runtime.wait(handle)


async def test_catalog_only_freeze_and_only_used_hash_verified(case):
    fs = TrackingFS({"/documents/used": b"used", "/documents/unused": b"unused"})
    snapshot = await freeze_vfs(fs, case.store, case.profile.limits, 3600, bindings=case.bindings)
    fs._files["/documents/unused"] = b"corrupted unused file"
    assert fs.reads == []
    lazy = LazyFiles(
        case.store, case.bindings, {f.path: f for f in snapshot.files}, case.profile.limits
    )
    assert await lazy.read("/documents/used") == b"used"
    assert fs.reads == ["/documents/used"]
    with pytest.raises(RuntimeFault, match="input_changed|byte_budget"):
        await lazy.read("/documents/unused")


async def test_discover_large_skill_is_metadata_only_and_read_is_paged(case):
    fs = TrackingFS(
        {
            "/skills/synthetic/SKILL.md": (
                b"---\nname: synthetic\ndescription: Fictional skill\n---\n" + b"x" * 50000
            ),
        }
    )

    async def inspect(prompt, gateway, checkpoint):
        found = await gateway.invoke("discover_skills", {}, "discover")
        assert found["value"][0]["name"] == "synthetic"
        assert fs.reads == []
        offset, body = 0, ""
        while True:
            page = await gateway.invoke(
                "read_file",
                {"path": "/skills/synthetic/SKILL.md", "offset": offset, "length": 8000},
                f"read-{offset}",
            )
            body += page["value"]["text"]
            offset = page["value"]["next_offset"]
            if page["value"]["eof"]:
                break
        assert len(body) > 50000
        assert len(fs.reads) == 1
        await gateway.invoke("submit_result", {"files": ["/output/synthetic.bin"]}, "submit")
        return b"[]"

    case.harness.run = inspect
    _, result = await run(case, "skills", vfs=fs)
    assert result.status == "SUCCEEDED", result.error


async def test_renewed_access_lease_has_same_handle_and_content_fingerprint(case):
    async def no_start(*args):
        pass

    case.scheduler.start = no_start
    kwargs = dict(prompt="Fictional renew", vfs=case.vfs, tools=case.tools, idempotency_key="renew")
    first = await case.runtime.submit(**kwargs)
    case.vfs._expires += 120
    assert await case.runtime.submit(**kwargs) == first
    record = case.store.get_record("run/" + first.run_id)[1]
    body = case.store.read(BlobRef.model_validate(record["job_ref"]))
    assert b"expires_at" not in body
    changed = MemoryFS({"/documents/fictional.txt": b"different content"})
    with pytest.raises(RuntimeFault, match="idempotency_conflict"):
        await case.runtime.submit(**(kwargs | {"vfs": changed}))


async def test_two_rounds_new_worker_empty_delta_original_paths_and_previous(case):
    c = case
    fs = TrackingFS({"/documents/used": b"Fictional original input"})
    seen_checkpoints = []

    async def inspect(prompt, gateway, checkpoint):
        seen_checkpoints.append(checkpoint)
        assert "/documents/used" in gateway.files
        paths = ["/documents/used"]
        if checkpoint is not None:
            assert "/previous/synthetic.bin" in gateway.files
            paths.append("/previous/synthetic.bin")
        await gateway.lazy.materialize(gateway.box, paths)
        assert gateway.box.received["/documents/used"] == b"Fictional original input"
        if checkpoint:
            assert gateway.box.received["/previous/synthetic.bin"].startswith(b"entirely fictional")
        await gateway.invoke("submit_result", {"files": ["/output/synthetic.bin"]}, "submit")
        return b"[]"

    c.harness.run = inspect
    _, first = await run(c, "first", vfs=fs)
    assert first.status == "SUCCEEDED"
    new_store = LocalArtifactStore(c.store.root.parent, c.store.scope)
    new_bindings = WorkerBindings()
    new_bindings.install_profile(c.profile)
    new_bindings.install_tools(c.tools)
    new_executor = Executor(
        store=new_store, bindings=new_bindings, sandbox=c.provider, harness=c.harness
    )
    c.scheduler.executor = new_executor
    c.runtime = Runtime(
        config=RuntimeConfig(c.profile, c.scheduler), store=new_store, bindings=new_bindings
    )
    # Used input is archived locally. An untouched retained input instead needs its provider
    # reinstalled on the new worker; no promise that MemoryFS survives process termination.
    _, second = await run(c, "second", vfs=MemoryFS({}), revision=1)
    assert second.status == "SUCCEEDED", second.error
    assert seen_checkpoints == [None, b"[]"]
    assert len(c.provider.boxes) == 2 and c.provider.boxes[0] is not c.provider.boxes[1]
    version = second.session_version
    assert not c.runtime.snapshot(version.submitted_input_ref).files
    assert len(c.runtime.snapshot(version.effective_input_ref).files) == 1
    assert version.parent_manifest_ref == first.manifest_ref


async def test_override_tombstone_and_replace_are_explicit(case):
    c = case
    _, first = await run(c, "first")
    assert first.status == "SUCCEEDED"
    new = MemoryFS({"/documents/fictional.txt": b"New fictional input"})
    with pytest.raises(RuntimeFault, match="input_override_required"):
        await run(c, "without-override", vfs=new, revision=1)
    old_hash = digest(b"Entirely fictional specimen")
    _, second = await run(
        c, "override", vfs=new, revision=1, overrides={"/documents/fictional.txt": old_hash}
    )
    assert second.status == "SUCCEEDED"
    with pytest.raises(RuntimeFault, match="input_override_conflict"):
        await run(
            c,
            "wrong-delete",
            vfs=MemoryFS({}),
            revision=2,
            tombstones={"/documents/fictional.txt": old_hash},
        )
    _, third = await run(
        c,
        "delete",
        vfs=MemoryFS({}),
        revision=2,
        tombstones={"/documents/fictional.txt": digest(b"New fictional input")},
    )
    assert third.status == "SUCCEEDED"
    assert not c.runtime.snapshot(third.session_version.effective_input_ref).files
    _, fourth = await run(c, "replace", vfs=new, revision=3, input_mode="replace")
    assert fourth.status == "SUCCEEDED"


async def two_outputs(c):
    async def produce(prompt, gateway, checkpoint):
        c.harness.calls += 1
        gateway.box.outputs = {
            "/output/one": b"first fictional",
            "/output/two": b"second fictional",
        }
        await gateway.invoke("submit_result", {"files": ["/output/one", "/output/two"]}, "submit")
        return b"[]"

    c.harness.run = produce


async def test_second_save_failure_rebuild_collector_no_harness_no_model(case):
    c = case
    await two_outputs(c)
    original = c.store.blob_store.put

    def fail_second(data):
        if data == b"second fictional":
            raise OSError("synthetic second blob failure")
        return original(data)

    c.store.blob_store.put = fail_second
    handle, pending = await run(c, "two-files")
    assert pending.status == "RECOVERY_PENDING"
    state = Collector(c.store).state(handle.run_id)
    assert set(state["receipts"]) == {"/output/one"}
    assert not c.provider.boxes[0].destroyed
    assert c.harness.calls == 1
    store = LocalArtifactStore(c.store.root.parent, c.store.scope)
    collector_worker = Executor(store=store, bindings=None, sandbox=c.provider, harness=None)
    recovered = await collector_worker.recover_collection(
        handle.run_id, state["seal_ref"]["sha256"]
    )
    assert recovered["status"] == "SUCCEEDED"
    assert c.harness.calls == 1
    assert c.provider.boxes[0].reads.count("/output/one") == 1
    assert c.provider.boxes[0].destroyed
    assert (await c.runtime.get(handle)).status == "SUCCEEDED"
    assert len(c.runtime.session_versions("synthetic-session")) == 1
    # A repeated collector invocation reconciles the already committed result.
    again = await collector_worker.recover_collection(handle.run_id, state["seal_ref"]["sha256"])
    assert again["status"] == "SUCCEEDED" and c.harness.calls == 1


async def test_collector_receipt_response_loss_reconciles_without_second_download(case):
    c = case
    await two_outputs(c)
    original = c.store.cas
    tripped = []

    def lose(key, expected, value, db=None):
        if key.startswith("collection/") and len(value.get("receipts", {})) == 1 and not tripped:
            tripped.append(True)
            raise OSError("synthetic receipt failure")
        return original(key, expected, value, db)

    c.store.cas = lose
    handle, pending = await run(c, "receipt-loss")
    assert pending.status == "RECOVERY_PENDING"
    c.store.cas = original
    state = Collector(c.store).state(handle.run_id)
    await c.executor.recover_collection(handle.run_id, state["seal_ref"]["sha256"])
    assert c.harness.calls == 1
    assert c.provider.boxes[0].reads.count("/output/one") == 1


async def test_real_function_model_second_file_failure_recovery_keeps_request_count(case):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from foliorun.adapters.pydantic_ai import PydanticHarness

    c = case
    requests = []

    def respond(messages, info):
        requests.append(1)
        if messages[-1].parts[0].part_kind == "user-prompt":
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "submit_result", {"files": ["/output/one", "/output/two"]}, "submit"
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("Submitted")])

    class Client:
        async def close(self):
            pass

    create = c.provider.create

    async def with_outputs(*args):
        box = await create(*args)
        box.outputs = {"/output/one": b"first", "/output/two": b"second"}
        return box

    c.provider.create = with_outputs
    c.executor.harness = PydanticHarness(
        model_factory=lambda: (Client(), FunctionModel(respond, model_name=c.profile.model))
    )
    original = c.store.blob_store.put

    def fail(data):
        if data == b"second":
            raise OSError("synthetic second write failure")
        return original(data)

    c.store.blob_store.put = fail
    handle, pending = await run(c, "real-model-collection")
    assert pending.status == "RECOVERY_PENDING" and len(requests) == 2
    c.store.blob_store.put = original
    state = Collector(c.store).state(handle.run_id)
    worker = Executor(store=c.store, bindings=None, sandbox=c.provider, harness=None)
    await worker.recover_collection(handle.run_id, state["seal_ref"]["sha256"])
    assert len(requests) == 2
    assert (await c.runtime.get(handle)).status == "SUCCEEDED"


async def test_expired_collection_is_review_and_cleanup_not_regeneration(case):
    c = case
    await two_outputs(c)
    original = c.store.blob_store.put

    def fail(data):
        if data == b"second fictional":
            raise OSError("synthetic")
        return original(data)

    c.store.blob_store.put = fail
    handle, pending = await run(c, "expiry")
    assert pending.status == "RECOVERY_PENDING"
    key = "collection/" + handle.run_id
    old = c.store.get_record(key)
    c.store.cas(key, old[0], {**old[1], "retain_until": int(time.time()) - 1})
    result = await c.executor.recover_collection(handle.run_id, old[1]["seal_ref"]["sha256"])
    assert result["error"] == "output_source_lost" and result["status"] == "NEEDS_REVIEW"
    assert c.harness.calls == 1 and c.provider.boxes[0].destroyed
    assert c.runtime.sessions.get("synthetic-session")["state"] == "review"
    with pytest.raises(RuntimeFault, match="session_busy"):
        c.runtime.sessions.delete("synthetic-session", expected_revision=0)


def configure_fallback(c):
    data = c.profile.model_dump()
    data["fallback"] = FallbackPolicy(max_attempts=1).model_dump()
    c.profile = Profile.model_validate(data)
    c.bindings.install_profile(c.profile)
    c.runtime.config = RuntimeConfig(c.profile, c.scheduler)


async def test_native_restore_failure_explicit_new_run_from_last_success(case):
    c = case
    configure_fallback(c)
    _, first = await run(c, "first")
    assert first.status == "SUCCEEDED"

    def fail_restore(body):
        raise RuntimeFault("checkpoint_invalid")

    c.harness.restore = fail_restore
    handle, result = await run(c, "second", vfs=MemoryFS({}), revision=1)
    assert result.status == "SUCCEEDED", result.error
    assert result.handle.run_id != handle.run_id and result.attempt_of == handle.run_id
    assert c.harness.calls == 2
    assert len(c.provider.boxes) == 2
    assert "/documents/fictional.txt" in c.provider.boxes[1].received
    assert "/previous/synthetic.bin" in c.provider.boxes[1].received
    parent = c.store.get_record("run/" + handle.run_id)[1]
    assert parent["next_attempt"] == result.handle.run_id
    assert parent["result"]["error"] == "native_restore_failed"
    assert result.session_version.revision == 2


async def test_fallback_refuses_external_effect_without_idempotency_or_permission(case):
    c = case
    configure_fallback(c)
    spec, handler = c.tools.entries["lookup_record"]
    tools = ToolRegistry("external", "1", release="synthetic-external")
    tools.register(spec.model_copy(update={"effect": "external_side_effect"}), handler)
    c.tools = tools
    c.bindings.install_tools(tools)
    _, first = await run(c, "first")
    assert first.status == "SUCCEEDED"

    def fail_restore(body):
        raise RuntimeFault("checkpoint_invalid")

    c.harness.restore = fail_restore
    handle, result = await run(c, "second", vfs=MemoryFS({}), revision=1)
    assert result.status == "FAILED" and result.error == "native_restore_failed"
    assert not c.store.get_record("run/" + handle.run_id)[1].get("next_attempt")
    assert c.harness.calls == 1


@pytest.mark.parametrize(
    "retry_class,repeat",
    [
        ("idempotency_required", False),
        ("never_auto", True),
    ],
)
async def test_external_fallback_requires_host_idempotency_or_repeat_permission(
    case, retry_class, repeat
):
    c = case
    configure_fallback(c)
    c.profile = c.profile.model_copy(
        update={"fallback": FallbackPolicy(max_attempts=1, allow_external_repetition=repeat)}
    )
    c.bindings.install_profile(c.profile)
    c.runtime.config = RuntimeConfig(c.profile, c.scheduler)
    spec, handler = c.tools.entries["lookup_record"]
    tools = ToolRegistry("guarded-external", release="synthetic")
    tools.register(
        spec.model_copy(update={"effect": "external_side_effect", "retry_class": retry_class}),
        handler,
    )
    c.tools = tools
    c.bindings.install_tools(tools)
    _, first = await run(c, "first")
    assert first.status == "SUCCEEDED"

    def fail(body):
        raise RuntimeFault("checkpoint_invalid")

    c.harness.restore = fail
    parent, result = await run(c, "second", revision=1)
    assert result.status == "SUCCEEDED" and result.attempt_of == parent.run_id


async def test_unknown_model_or_tool_window_never_fallback(case):
    c = case
    configure_fallback(c)

    async def unknown(*args):
        c.harness.calls += 1
        raise RuntimeFault("unknown_model_window")

    c.harness.run = unknown
    handle, result = await run(c, "unknown")
    assert result.status == "NEEDS_REVIEW"
    assert c.executor.reserve_fallback(handle.run_id) is None
    assert c.harness.calls == 1


async def test_stale_publication_fence_cannot_commit(case):
    c = case
    c.scheduler.hold.clear()
    handle = await c.runtime.submit(
        prompt="Fictional fence",
        vfs=c.vfs,
        tools=c.tools,
        idempotency_key="fence",
        session_id="synthetic-session",
    )
    while not c.store.get_record("run/" + handle.run_id)[1]["prepared"]:
        await asyncio.sleep(0)
    old = c.store.get_record("session/synthetic-session")
    c.store.cas("session/synthetic-session", old[0], {**old[1], "fence": old[1]["fence"] + 1})
    c.scheduler.hold.set()
    result = await c.runtime.wait(handle)
    assert result.error == "publication_fenced" and not result.session_version
    assert c.runtime.sessions.get("synthetic-session")["revision"] == 0
