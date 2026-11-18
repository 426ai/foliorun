import asyncio
import time
from types import SimpleNamespace

import pytest

from foliorun import (
    LocalArtifactStore,
    MemoryFS,
    Profile,
    Runtime,
    RuntimeConfig,
    ToolRegistry,
    ToolSpec,
    WorkerBindings,
)
from foliorun.execution import Executor


class FakeBox:
    id = "synthetic-unit-box"

    def __init__(self):
        self.destroyed = False
        self.received = None
        self.outputs = {
            p: b"entirely fictional binary\x00\xff\n"
            for p in ("/output/synthetic.bin", "/output/x")
        }
        self.sealed = False
        self.retain_until = int(time.time()) + 3600
        self.reads = []

    async def prepare(self, files, store, profile):
        self.received = dict.fromkeys(files)

    async def materialize(self, path, body, file):
        self.received[path] = body

    async def seal(self, paths, limits):
        from foliorun.contracts import digest, path_checked

        for p in paths:
            path_checked(p, output=True)
        self.sealed = True
        return [
            {"path": p, "sha256": digest(self.outputs[p]), "size": len(self.outputs[p])}
            for p in paths
        ]

    async def read_output(self, path, size):
        self.reads.append(path)
        assert self.sealed
        return self.outputs[path]

    async def verify_retention(self, until):
        assert self.retain_until >= until

    async def close(self):
        pass

    async def destroy(self):
        self.destroyed = True


class FakeProvider:
    from foliorun.contracts import AdapterDescriptor

    descriptor = AdapterDescriptor(name="opensandbox", version="0.1.16")

    def __init__(self):
        self.boxes = []

    async def create(self, *args):
        b = FakeBox()
        b.id = "synthetic-unit-box-" + str(len(self.boxes))
        self.boxes.append(b)
        return b

    async def attach_owned(self, sandbox_id, run_id, scope):
        for box in self.boxes:
            if box.id == sandbox_id and not box.destroyed:
                return box
        from foliorun import RuntimeFault

        raise RuntimeFault("output_source_lost")

    async def destroy_owned(self, sandbox_id, run_id, scope):
        for box in self.boxes:
            if box.id == sandbox_id:
                await box.destroy()


class FakeHarness:
    calls = 0

    def validate_profile(self, profile):
        pass

    def validate_checkpoint(self, checkpoint):
        pass

    def restore(self, body):
        return body

    def checkpoint(self, native, output):
        from foliorun.contracts import Checkpoint

        return Checkpoint(
            harness="synthetic",
            adapter_version="1",
            harness_version="1",
            format_version="fake",
            checkpoint_ref=native,
            file_snapshot_ref=output,
        )

    async def run(self, prompt, gateway, checkpoint):
        self.calls += 1
        await gateway.invoke("submit_result", {"files": ["/output/synthetic.bin"]}, "submit-1")
        return b'[{"synthetic_checkpoint":true}]'


class LocalTestScheduler:
    """Test orchestration only; never advertised as a supported production adapter."""

    def __init__(self, executor):
        self.executor = executor
        self.tasks, self.statuses = {}, {}
        self.hold = asyncio.Event()
        self.hold.set()

    async def start(self, handle, ref):
        if handle.run_id in self.tasks:
            return

        async def run():
            self.statuses[handle.run_id] = "RUNNING"
            try:
                run_id, job_ref = handle.run_id, ref
                for _ in range(6):
                    await self.executor.execute(run_id, job_ref)
                    child = self.executor.reserve_fallback(run_id)
                    if child is None:
                        break
                    from foliorun.contracts import BlobRef

                    run_id, job_ref = child["run_id"], BlobRef.model_validate(child["job_ref"])
            except Exception as e:
                self.executor._failure(
                    handle.run_id, getattr(e, "code", "unknown_execution_window")
                )
            await self.hold.wait()
            self.statuses[handle.run_id] = self.executor.finalize(run_id)

        self.tasks[handle.run_id] = asyncio.create_task(run())

    async def status(self, handle):
        return self.statuses.get(handle.run_id, "QUEUED")

    async def wait(self, handle, timeout):
        await asyncio.wait_for(asyncio.shield(self.tasks[handle.run_id]), timeout)

    async def cancel(self, handle):
        return self.executor.cancel_fence(handle.run_id)


@pytest.fixture
def case(tmp_path):
    store = LocalArtifactStore(tmp_path / "store", "fictional-tenant-a")
    profile = Profile(
        name="demo",
        version="1",
        image="localhost/demo@sha256:" + "a" * 64,
        image_manifest="b" * 64,
        skills_manifest="c" * 64,
        allowed_tools=("lookup_record",),
        retention_seconds=3600,
    )
    tools = ToolRegistry("demo", "1", release="synthetic-1")

    async def lookup(args, ctx):
        return {"kind": "json", "value": {"id": args["id"]}}

    tools.register(
        ToolSpec(
            name="lookup_record",
            description="Synthetic lookup",
            version="1",
            execution_location="host",
            effect="read_only",
            input_schema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "additionalProperties": False,
                "required": ["id"],
            },
        ),
        lookup,
    )
    bindings = WorkerBindings()
    bindings.install_profile(profile)
    bindings.install_tools(tools)
    provider, harness = FakeProvider(), FakeHarness()
    executor = Executor(store=store, bindings=bindings, sandbox=provider, harness=harness)
    scheduler = LocalTestScheduler(executor)
    runtime = Runtime(config=RuntimeConfig(profile, scheduler), bindings=bindings, store=store)
    vfs = MemoryFS({"/documents/fictional.txt": b"Entirely fictional specimen"})
    return SimpleNamespace(**locals())
