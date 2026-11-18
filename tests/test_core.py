import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from foliorun import (
    FilesFS,
    LocalArtifactStore,
    MemoryFS,
    RuntimeFault,
    WaitTimeout,
)
from foliorun.contracts import BlobRef, JobSpec, digest, path_checked
from foliorun.execution import Executor
from foliorun.tools import Gateway, Journal, ToolRegistry
from foliorun.vfs import LazyFiles, freeze_vfs


async def submit(c, **kwargs):
    return await c.runtime.submit(
        prompt="Entirely fictional test", vfs=c.vfs, tools=c.tools, idempotency_key="test", **kwargs
    )


@pytest.mark.parametrize(
    "path",
    [
        "/output/../bad",
        "/documents/../x",
        "/workspace/x",
        "relative",
        "/documents//x",
        "/documents/./x",
        "/output/x\x00",
        "/previous/x",
        "/documents/a\\b",
    ],
)
def test_reject_paths(path):
    with pytest.raises(RuntimeFault):
        path_checked(path)


async def test_complete_download_and_rebuild_two_versions(case, tmp_path):
    c = case
    h = await submit(c, session_id="synthetic-session")
    r = await c.runtime.wait(h)
    assert r.status == "SUCCEEDED" and r.cleanup == "destroyed"
    assert c.provider.boxes[0].destroyed
    old = c.store.read(r.artifacts[0].blob)
    await c.store.download_to(r.artifacts[0], tmp_path / "saved.bin", expires_at=r.expires_at)
    assert (tmp_path / "saved.bin").read_bytes() == old
    with pytest.raises(FileExistsError):
        await c.store.download_to(r.artifacts[0], tmp_path / "saved.bin", expires_at=r.expires_at)
    c.scheduler.executor = Executor(
        store=c.store, bindings=c.bindings, sandbox=c.provider, harness=c.harness
    )
    h2 = await c.runtime.submit(
        prompt="Revise fictional specimen",
        vfs=c.vfs,
        tools=c.tools,
        idempotency_key="round2",
        session_id="synthetic-session",
        session_revision=1,
    )
    r2 = await c.runtime.wait(h2)
    assert r2.status == "SUCCEEDED" and h2.run_id != h.run_id
    assert "/previous/synthetic.bin" in c.provider.boxes[1].received
    assert len(c.runtime.session_versions("synthetic-session")) == 2
    assert c.store.read(r.artifacts[0].blob) == old


async def test_idempotency_and_job_refs_only(case):
    c = case
    h = await submit(c)
    assert await submit(c) == h
    rec = c.store.get_record("run/" + h.run_id)[1]
    job = JobSpec.model_validate(c.store.read_json(BlobRef.model_validate(rec["job_ref"])))
    assert "Entirely fictional test" not in job.model_dump_json()
    assert job.prompt_ref.sha256 == digest(b"Entirely fictional test")
    with pytest.raises(RuntimeFault, match="idempotency_conflict"):
        await c.runtime.submit(
            prompt="Different fictional prompt", vfs=c.vfs, tools=c.tools, idempotency_key="test"
        )
    await c.runtime.wait(h)


async def test_session_concurrency_rejected(case):
    c = case
    c.scheduler.hold.clear()
    h = await submit(c, session_id="serial")
    with pytest.raises(RuntimeFault, match="session_conflict"):
        await c.runtime.submit(
            prompt="Other", vfs=c.vfs, tools=c.tools, idempotency_key="other", session_id="serial"
        )
    c.scheduler.hold.set()
    await c.runtime.wait(h)


async def test_wait_timeout_does_not_cancel_and_cancel_late_submit(case):
    c = case
    c.scheduler.hold.clear()
    h = await submit(c)
    with pytest.raises(WaitTimeout) as err:
        await c.runtime.wait(h, timeout=0.01)
    assert err.value.handle == h
    assert not c.store.get_record("run/" + h.run_id)[1]["cancelled"]
    assert await c.runtime.cancel(h)
    job = JobSpec.model_validate(
        c.store.read_json(
            BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
        )
    )
    gw = Gateway(c.store, h.run_id, job, c.profile, c.tools, None, {})
    with pytest.raises(RuntimeFault, match="cancelled"):
        await gw.invoke("submit_result", {"files": ["/output/late.bin"]}, "late")
    c.scheduler.hold.set()
    r = await c.runtime.wait(h)
    assert r.status == "CANCELLED" and not r.artifacts and not r.session_version
    assert not await c.runtime.cancel(h)


async def test_completed_preparation_reused_without_harness(case):
    c = case
    c.scheduler.hold.clear()
    h = await submit(c)
    while not c.store.get_record("run/" + h.run_id)[1]["prepared"]:
        await asyncio.sleep(0)
    ref = BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
    original = c.store.get_record("run/" + h.run_id)[1]["prepared"]
    new = Executor(store=c.store, bindings=None, sandbox=None, harness=None)
    assert await new.execute(h.run_id, ref) == original
    assert len(c.provider.boxes) == 1
    c.scheduler.hold.set()
    await c.runtime.wait(h)


async def test_unknown_execution_stops_without_new_box(case):
    c = case
    c.scheduler.hold.clear()

    # Reserve a run without scheduling its execution.
    async def no_start(*args):
        pass

    c.scheduler.start = no_start
    h = await submit(c)
    c.executor.update_run(h.run_id, claimed=True)
    ref = BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
    r = await c.executor.execute(h.run_id, ref)
    assert r["status"] == "NEEDS_REVIEW" and not c.provider.boxes


async def test_missing_binding_fail_closed(case):
    c = case
    c.bindings.registries.clear()
    with pytest.raises(RuntimeFault, match="binding_unavailable"):
        await submit(c)
    assert not c.provider.boxes


async def test_worker_missing_profile_after_submit(case):
    c = case

    async def no_start(*args):
        pass

    c.scheduler.start = no_start
    h = await submit(c)
    c.bindings.profiles.clear()
    ref = BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
    with pytest.raises(RuntimeFault, match="binding_unavailable"):
        await c.executor.execute(h.run_id, ref)
    assert not c.provider.boxes


async def test_vfs_drift_and_stream_budget(case):
    class Drift(MemoryFS):
        async def stat(self, revision, path):
            r = await super().stat(revision, path)
            self._files[path] = b"Changed"
            return r

    snapshot = await freeze_vfs(
        Drift({"/documents/x": b"Before"}),
        case.store,
        case.profile.limits,
        3600,
        bindings=case.bindings,
    )
    lazy = LazyFiles(
        case.store, case.bindings, {f.path: f for f in snapshot.files}, case.profile.limits
    )
    with pytest.raises(RuntimeFault, match="input_changed|byte_budget"):
        await lazy.read("/documents/x")


async def test_list_stat_revision_and_skill_optional(case):
    revision = await case.vfs.freeze()
    with pytest.raises(RuntimeFault, match="input_changed"):
        await case.vfs.list("unknown", "/")
    assert await case.vfs.stat(revision.revision, "/documents/fictional.txt") == revision.entries[0]
    assert (await case.runtime.wait(await submit(case))).status == "SUCCEEDED"


def test_files_symlink_rejected(tmp_path):
    p = tmp_path / "source"
    p.write_bytes(b"Fictional")
    link = tmp_path / "link"
    link.symlink_to(p)
    with pytest.raises(RuntimeFault, match="symlink_input"):
        FilesFS({"/documents/x": link})
    fs = FilesFS({"/documents/x": p})
    p.write_bytes(b"Changed after capture")
    assert fs._files["/documents/x"] == b"Fictional"


def test_files_hardlink_rejected(tmp_path):
    import os

    source, link = tmp_path / "source", tmp_path / "hardlink"
    source.write_bytes(b"Fictional")
    os.link(source, link)
    with pytest.raises(RuntimeFault, match="invalid_input_file"):
        FilesFS({"/documents/x": link})


def test_store_scope_hash_and_cas(case, tmp_path):
    store = case.store
    ref = store.put(b"Synthetic\x00\xff\n")
    other = LocalArtifactStore(tmp_path / "store", "fictional-tenant-b")
    with pytest.raises(RuntimeFault, match="scope_mismatch"):
        other.read(ref)
    store.cas("x", 0, {"v": 1})
    with pytest.raises(RuntimeFault, match="cas_conflict"):
        store.cas("x", 0, {"v": 2})
    (store.blobs / ref.sha256).write_bytes(b"bad")
    with pytest.raises(RuntimeFault, match="blob_integrity"):
        store.read(ref)


def test_cas_concurrent_one_winner(case):
    def claim(i):
        try:
            case.store.cas("shared", 0, {"i": i})
            return True
        except RuntimeFault:
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(claim, range(8))) == 1


def test_journal_unknown_conflict_and_result_reuse(case):
    j = Journal(case.store, "demo")
    key, result = j.begin("model", "1", {"args": "fictional"})
    assert result is None
    with pytest.raises(RuntimeFault, match="unknown_model_window"):
        j.begin("model", "1", {"args": "fictional"})
    with pytest.raises(RuntimeFault, match="call_id_conflict"):
        j.begin("model", "1", {"args": "other"})
    j.complete(key, {"ref": "synthetic"})
    assert j.begin("model", "1", {"args": "fictional"})[1] == {"ref": "synthetic"}


@pytest.mark.parametrize("name", ["lookup_record", "fs_read", "sandbox_exec", "submit_result"])
def test_registry_duplicate_reserved_and_immutable(case, name):
    spec, handler = case.tools.entries["lookup_record"]
    registry = ToolRegistry("fresh", "1", release="demo")
    registry.register(spec, handler)
    if name == "lookup_record":
        with pytest.raises(RuntimeFault, match="tool_name_conflict"):
            registry.register(spec, handler)
    else:
        with pytest.raises(RuntimeFault, match="tool_name_conflict"):
            registry.register(spec.model_copy(update={"name": name}), handler)
    ref = case.tools.seal()
    copied = case.tools.entries["lookup_record"][0]
    copied.input_schema["properties"].clear()
    assert case.tools.seal() == ref
    with pytest.raises(RuntimeFault, match="registry_frozen"):
        case.tools.register(spec, handler)


def test_external_effect_explicit_and_location_rejected(case):
    spec, handler = case.tools.entries["lookup_record"]
    registry = ToolRegistry("x", "1", release="x")
    registry.register(spec.model_copy(update={"effect": "external_side_effect"}), handler)
    registry = ToolRegistry("y", "1", release="y")
    with pytest.raises(RuntimeFault, match="sandbox_mapping_required"):
        registry.register(spec.model_copy(update={"execution_location": "sandbox"}), handler)


async def test_gateway_authorization_schema_and_host(case):
    c = case

    async def no_start(*args):
        pass

    c.scheduler.start = no_start
    h = await submit(c)
    job = JobSpec.model_validate(
        c.store.read_json(
            BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
        )
    )
    gateway = Gateway(c.store, h.run_id, job, c.profile, c.tools, None, {})
    with pytest.raises(RuntimeFault, match="tool_not_authorized"):
        await gateway.invoke("shell", {}, "1")
    with pytest.raises(RuntimeFault, match="tool_arguments_invalid"):
        await gateway.invoke("lookup_record", {"id": "demo", "location": "sandbox"}, "2")
    assert (await gateway.invoke("lookup_record", {"id": "demo"}, "3"))["value"] == {"id": "demo"}
    with pytest.raises(RuntimeFault, match="call_id_conflict"):
        await gateway.invoke("lookup_record", {"id": "other"}, "3")


async def test_cleanup_failure_keeps_primary_result(case):
    c = case

    async def create(*args):
        from conftest import FakeBox

        box = FakeBox()

        async def fail():
            raise OSError("fictional cleanup failure")

        box.destroy = fail
        return box

    c.provider.create = create
    r = await c.runtime.wait(await submit(c))
    assert r.status == "SUCCEEDED" and r.cleanup == "pending"


async def test_manifest_save_failure_no_publication_and_cleanup(case):
    c = case
    original = c.store.put_json

    def fail_manifest(value):
        if isinstance(value, dict) and "files" in value and "run_id" in value:
            raise OSError("synthetic injected persistence failure")
        return original(value)

    c.store.put_json = fail_manifest
    handle = await submit(c)
    r = await c.runtime.wait(handle)
    assert r.status == "RECOVERY_PENDING" and not r.artifacts
    assert c.provider.boxes[0].destroyed
    c.store.put_json = original
    from foliorun.collection import Collector

    state = Collector(c.store).state(handle.run_id)
    await c.executor.recover_collection(handle.run_id, state["seal_ref"]["sha256"])
    assert (await c.runtime.get(handle)).status == "SUCCEEDED"
    assert c.harness.calls == 1


def test_closed_job_schema(case):
    with pytest.raises(ValidationError):
        BlobRef(scope="x", sha256="a" * 64, size=1, secret="forbidden synthetic value")


def test_input_fifo_and_system_paths_rejected(tmp_path):
    import os

    p = tmp_path / "fifo"
    os.mkfifo(p)
    with pytest.raises(RuntimeFault, match="invalid_input_file"):
        FilesFS({"/documents/x": p})
    for path in ["/etc/passwd", "/opt/document-tools/tool.py", "/root/x"]:
        with pytest.raises(RuntimeFault):
            path_checked(path)


async def test_download_parent_symlink_rejected(case, tmp_path):
    from foliorun import FileRef

    directory = tmp_path / "real"
    directory.mkdir()
    (tmp_path / "link").symlink_to(directory, target_is_directory=True)
    with pytest.raises(RuntimeFault, match="unsafe_local_path"):
        await case.store.download_to(
            FileRef(path="/output/x", blob=case.store.put(b"fictional")),
            tmp_path / "link/x",
            expires_at=9999999999,
        )
    assert not (directory / "x").exists()


async def test_cleanup_compensation_does_not_reenter_harness(case):
    c = case
    h = await submit(c)
    await c.runtime.wait(h)
    c.executor.update_run(h.run_id, cleanup="pending")
    calls = []

    async def destroy_owned(sandbox_id, run_id, scope):
        calls.append((sandbox_id, run_id, scope))

    c.provider.destroy_owned = destroy_owned
    assert await c.executor.cleanup(h.run_id) == "destroyed"
    assert len(calls) == 1 and len(c.provider.boxes) == 1
    assert (await c.runtime.get(h)).cleanup == "destroyed"


async def test_cancel_wins_after_collection_before_publish(case):
    c = case
    c.scheduler.hold.clear()
    h = await submit(c)
    while not c.store.get_record("run/" + h.run_id)[1]["prepared"]:
        await asyncio.sleep(0)
    assert c.store.get_record("run/" + h.run_id)[1]["prepared"]["manifest_ref"]
    assert await c.runtime.cancel(h)
    c.scheduler.hold.set()
    result = await c.runtime.wait(h)
    assert result.status == "CANCELLED" and not result.artifacts and not result.manifest_ref


async def test_half_materialization_disconnect_cleans_without_model(case):
    c = case
    calls = []

    async def create(*args):
        from conftest import FakeBox

        box = FakeBox()

        async def prepare(*args):
            calls.append("partial_upload")
            raise ConnectionError("synthetic injected disconnect")

        box.prepare = prepare
        c.provider.boxes.append(box)
        return box

    c.provider.create = create
    result = await c.runtime.wait(await submit(c))
    assert result.status == "NEEDS_REVIEW" and c.provider.boxes[0].destroyed
    assert calls == ["partial_upload"]


async def test_host_result_persistence_failure_unknown_not_reexecuted(case):
    c = case
    calls = []
    # Use a fresh authorized registry with a counted, read-only service.
    registry = ToolRegistry("counted", "1", release="counted-1")
    spec, _ = c.tools.entries["lookup_record"]

    async def counted(args, ctx):
        calls.append("sent")
        return {"kind": "json", "value": "fictional"}

    registry.register(spec, counted)
    c.bindings.install_tools(registry)
    c.scheduler.start = no_start
    h = await c.runtime.submit(
        prompt="Fictional", vfs=c.vfs, tools=registry, idempotency_key="host-fault"
    )
    job = JobSpec.model_validate(
        c.store.read_json(
            BlobRef.model_validate(c.store.get_record("run/" + h.run_id)[1]["job_ref"])
        )
    )
    gw = Gateway(c.store, h.run_id, job, c.profile, registry, None, {})

    def fail(*args):
        raise OSError("synthetic result journal failure")

    gw.journal.complete = fail
    with pytest.raises(OSError):
        await gw.invoke("lookup_record", {"id": "demo"}, "host-1")
    with pytest.raises(RuntimeFault, match="unknown_tool_window"):
        await gw.invoke("lookup_record", {"id": "demo"}, "host-1")
    assert calls == ["sent"]


async def no_start(*args):
    pass


def test_registry_identity_cannot_change_after_seal(case):
    with pytest.raises(RuntimeFault, match="registry_frozen"):
        case.tools.release = "changed"


async def test_prefix_collision_and_expired_snapshot_rejected(case):
    with pytest.raises(RuntimeFault, match="input_changed"):
        await freeze_vfs(
            MemoryFS({"/documents/a": b"a", "/documents/a/b": b"b"}),
            case.store,
            case.profile.limits,
            3600,
        )
    fs = MemoryFS({"/documents/a": b"a"}, retention_seconds=-1)
    with pytest.raises(RuntimeFault, match="input_changed|input_retention_too_short"):
        await freeze_vfs(fs, case.store, case.profile.limits, 3600)
