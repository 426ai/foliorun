"""Real adapter dispatch against a controlled transport; injected storage, GC and fencing."""

import asyncio
import io
import shlex
import sys
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from foliorun import LocalSessionRepository, RuntimeFault, S3BlobStore, Storage
from foliorun.adapters import opensandbox as adapter
from foliorun.collection import Collector
from foliorun.contracts import canonical, digest
from foliorun.tools import SandboxCommand


async def test_provider_really_dispatches_two_different_preinstalled_programs(case, monkeypatch):
    commands, creates = [], []
    upper = (
        "import sys,json; x=json.loads(sys.argv[1]); "
        "print(json.dumps({'files':['/output/upper.txt'],'value':x['text'].upper()}))"
    )
    reverse = (
        "import sys,json; x=json.loads(sys.argv[1]); "
        "print(json.dumps({'files':['/output/reverse.txt'],'value':x['text'][::-1]}))"
    )

    async def command(text, opts):
        argv = shlex.split(text)
        assert argv[:4] == ["runuser", "-u", "app", "--"]
        commands.append(argv[4:])
        process = await asyncio.create_subprocess_exec(
            *argv[4:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PYTHONDONTWRITEBYTECODE": "1"},
        )
        stdout, _ = await process.communicate()
        return SimpleNamespace(error=None, exit_code=process.returncode, text=stdout.decode())

    async def info():
        return SimpleNamespace(expires_at=datetime.now(UTC) + timedelta(hours=2))

    async def create(image, **kwargs):
        creates.append(kwargs)
        return SimpleNamespace(
            id="synthetic-sdk-box", commands=SimpleNamespace(run=command), get_info=info
        )

    monkeypatch.setattr(adapter.Sandbox, "create", create)
    provider = adapter.OpenSandboxProvider(
        api_key="synthetic-not-a-credential",
        endpoint="example.test",
        tools={
            "uppercase": adapter.PreinstalledTool((sys.executable, "-c", upper)),
            "reverse": adapter.PreinstalledTool((sys.executable, "-c", reverse)),
        },
    )
    box = await provider.create(case.profile, "synthetic-run", case.store.scope)
    first = await box.execute(
        SandboxCommand(entrypoint="uppercase"), {"text": "Fictional"}, "one", case.profile.limits
    )
    second = await box.execute(
        SandboxCommand(entrypoint="reverse"), {"text": "Fictional"}, "two", case.profile.limits
    )
    assert first["value"]["value"] == "FICTIONAL"
    assert second["value"]["value"] == "lanoitciF"
    assert first["value"]["files"] != second["value"]["files"]
    assert commands[0][2] == upper and commands[1][2] == reverse
    assert creates[0]["timeout"].total_seconds() >= (
        case.profile.limits.deadline_seconds + case.profile.collection_seconds
    )
    with pytest.raises(RuntimeFault, match="sandbox_binding_unavailable"):
        await box.execute(SandboxCommand(entrypoint="missing"), {}, "three", case.profile.limits)
    assert len(commands) == 2


async def test_provider_rejects_service_ttl_shorter_than_recovery_window():
    async def info():
        return SimpleNamespace(expires_at=datetime.now(UTC) + timedelta(seconds=5))

    box = adapter.OpenSandboxSession(SimpleNamespace(id="synthetic", get_info=info))
    with pytest.raises(RuntimeFault, match="sandbox_retention_unavailable"):
        await box.verify_retention(int(time.time()) + 3600)


class ConditionalClient:
    def __init__(self):
        self.data, self.calls = {}, []

    def put_object(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["IfNoneMatch"] == "*"
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.data:
            error = RuntimeError("synthetic precondition")
            error.response = {"Error": {"Code": "PreconditionFailed"}}
            raise error
        self.data[key] = kwargs["Body"]

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.data[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.data.pop((Bucket, Key), None)


def test_injected_s3_client_bucket_prefix_and_transactional_repository(tmp_path):
    client = ConditionalClient()
    blob = S3BlobStore(
        client=client, bucket="fictional-bucket", prefix="caller/selected", scope="fictional-scope"
    )
    index = LocalSessionRepository(tmp_path / "index", "fictional-scope")
    store = Storage(blobs=blob, sessions=index)
    ref = store.put(b"fictional bytes")
    assert store.put(b"fictional bytes") == ref
    assert store.read(ref) == b"fictional bytes"
    assert all(
        k[0] == "fictional-bucket" and k[1].startswith("caller/selected/fictional-scope/blobs/")
        for k in client.data
    )
    store.cas("session/synthetic", 0, {"ref": ref.model_dump()})
    with pytest.raises(RuntimeFault, match="cas_conflict"):
        store.cas("session/synthetic", 0, {})
    with pytest.raises(RuntimeFault, match="scope_mismatch"):
        blob.read(ref.model_copy(update={"scope": "other"}))
    assert store.gc() == ()


def age_catalog(store):
    for key, revision, value in store.repository.scan("blob/"):
        store.cas(key, revision, {**value, "protected_until": 0, "orphan_since": 0})


async def test_gc_session_artifact_and_run_roots_are_independent(case, tmp_path):
    c = case
    result = await c.runtime.run(
        prompt="Fictional GC",
        vfs=c.vfs,
        tools=c.tools,
        idempotency_key="gc",
        session_id="gc-session",
    )
    assert result.status == "SUCCEEDED"
    ref = result.artifacts[0].blob
    orphan = c.store.put(b"fictional orphan")
    c.runtime.sessions.close("gc-session", expected_revision=1)
    c.runtime.sessions.delete("gc-session", expected_revision=1)
    age_catalog(c.store)
    removed = c.store.gc()
    assert orphan.sha256 in removed and ref.sha256 not in removed
    run_key = "run/" + result.handle.run_id
    record = c.store.get_record(run_key)
    c.store.cas(run_key, record[0], {**record[1], "expires_at": 0})
    c.store.release_run(result.handle.run_id)
    assert ref.sha256 not in c.store.gc()
    destination = tmp_path / "delivered.bin"
    await c.runtime.artifacts.download_to(result.artifacts[0], destination)
    assert destination.read_bytes() == c.store.read(ref)
    key = "artifact/" + digest(canonical(result.artifacts[0]))
    receipt = c.store.get_record(key)
    c.store.cas(key, receipt[0], {**receipt[1], "expires_at": 0})
    c.store.expire_artifacts()
    age_catalog(c.store)
    assert ref.sha256 in c.store.gc()
    with pytest.raises(RuntimeFault, match="artifact_not_committed"):
        await c.runtime.artifacts.download_to(result.artifacts[0], tmp_path / "denied")


def test_new_managed_put_repins_old_orphan_before_gc(case):
    ref = case.store.put(b"fictional re-reference")
    age_catalog(case.store)
    # A fresh managed put/read-back obtains the same index exclusion as GC.
    assert case.store.put(b"fictional re-reference") == ref
    assert ref.sha256 not in case.store.gc()


def test_cannot_resurrect_deleted_blob_with_new_root(case):
    ref = case.store.put(b"fictional deleted orphan")
    age_catalog(case.store)
    assert ref.sha256 in case.store.gc()
    with pytest.raises(RuntimeFault, match="blob_unavailable"):
        case.store.cas("root/resurrection", 0, {"ref": ref.model_dump()})
    assert case.store.get_record("root/resurrection") is None


async def test_collector_lease_concurrency_and_late_fence(case):
    c = case
    result = await c.runtime.run(
        prompt="Fictional lease", vfs=c.vfs, tools=c.tools, idempotency_key="lease"
    )
    collector = Collector(c.store)
    run_id = result.handle.run_id
    state = collector.state(run_id)
    token = collector._claim(run_id, state["seal_ref"]["sha256"])
    with pytest.raises(RuntimeFault, match="collector_busy"):
        collector._claim(run_id, state["seal_ref"]["sha256"])
    key = "collection/" + run_id
    old = c.store.get_record(key)
    c.store.cas(key, old[0], {**old[1], "lease": {**token, "expires_at": 0}})
    new = collector._claim(run_id, state["seal_ref"]["sha256"])
    assert new["fence"] > token["fence"]
    with pytest.raises(RuntimeFault, match="collector_fenced"):
        collector._update(run_id, token, phase="complete")
    collector._update(run_id, new, lease=None)


async def test_cancel_before_fallback_reservation_cannot_start_child(case):
    from test_revision import configure_fallback, run

    c = case
    configure_fallback(c)
    _, first = await run(c, "first")
    assert first.status == "SUCCEEDED"

    def fail_restore(body):
        raise RuntimeFault("checkpoint_invalid")

    c.harness.restore = fail_restore
    original = c.executor.reserve_fallback

    def cancel_then_reserve(run_id):
        assert c.executor.cancel_fence(run_id)
        return original(run_id)

    c.executor.reserve_fallback = cancel_then_reserve
    handle, result = await run(c, "second", revision=1)
    assert result.status == "CANCELLED"
    assert not c.store.get_record("run/" + handle.run_id)[1].get("next_attempt")
    assert c.harness.calls == 1
