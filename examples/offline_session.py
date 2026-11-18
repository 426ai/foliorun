"""Two-round Runtime demo. FunctionModel and synthetic sandbox/scheduler, no live services.

The demo transports are deliberately not production adapters. The Harness, Gateway,
VFS, storage, collector, revision publication and restoration are real Foliorun code.
"""

import asyncio
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from foliorun import (
    LocalArtifactStore,
    MemoryFS,
    Profile,
    Runtime,
    RuntimeConfig,
    ToolRegistry,
    WorkerBindings,
)
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.contracts import AdapterDescriptor, BlobRef, digest, path_checked
from foliorun.execution import Executor


class SyntheticBox:
    def __init__(self):
        self.id = uuid4().hex
        self.workspace = TemporaryDirectory(prefix="foliorun-synthetic-box-")
        self.root = Path(self.workspace.name)
        self.retain_until = int(time.time()) + 3600
        self.sealed = False

    async def prepare(self, files, store, profile):
        # Synthetic producer. A real preinstalled tool, not prepare(), creates its output.
        (self.root / "result.txt").write_bytes(b"Entirely fictional submitted file\n")

    async def materialize(self, path, body, file):
        target = self.root / path.removeprefix("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)

    async def seal(self, paths, limits):
        assert paths == ["/output/result.txt"]
        self.sealed = True
        body = (self.root / "result.txt").read_bytes()
        return [{"path": paths[0], "size": len(body), "sha256": digest(body)}]

    async def read_output(self, path, size):
        path_checked(path, output=True)
        assert self.sealed and path == "/output/result.txt"
        return (self.root / "result.txt").read_bytes()

    async def destroy(self):
        self.workspace.cleanup()

    async def close(self):
        pass


class SyntheticProvider:
    descriptor = AdapterDescriptor(name="synthetic-demo", version="1")

    def __init__(self):
        self.boxes = []

    async def create(self, profile, run_id, scope):
        box = SyntheticBox()
        self.boxes.append(box)
        return box


class SyntheticScheduler:
    def __init__(self, executor):
        self.executor, self.tasks, self.states = executor, {}, {}

    async def start(self, handle, job_ref):
        if handle.run_id in self.tasks:
            return

        async def execute():
            self.states[handle.run_id] = "RUNNING"
            run_id, ref = handle.run_id, job_ref
            for _ in range(6):
                await self.executor.execute(run_id, ref)
                child = self.executor.reserve_fallback(run_id)
                if child is None:
                    break
                run_id, ref = child["run_id"], BlobRef.model_validate(child["job_ref"])
            self.states[handle.run_id] = self.executor.finalize(run_id)

        self.tasks[handle.run_id] = asyncio.create_task(execute())

    async def wait(self, handle, timeout):
        await asyncio.wait_for(asyncio.shield(self.tasks[handle.run_id]), timeout)

    async def status(self, handle):
        return self.states.get(handle.run_id, "QUEUED")

    async def cancel(self, handle):
        return self.executor.cancel_fence(handle.run_id)


class SyntheticClient:
    async def close(self):
        pass


def respond(messages, info):
    part = messages[-1].parts[0]
    if part.part_kind == "user-prompt":
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "materialize_files", {"paths": ["/documents/specimen.txt"]}, "read-input"
                )
            ]
        )
    if part.part_kind == "tool-return" and part.tool_name == "materialize_files":
        return ModelResponse(
            parts=[ToolCallPart("submit_result", {"files": ["/output/result.txt"]}, "submit")]
        )
    return ModelResponse(parts=[TextPart("Submitted")])


async def main():
    with TemporaryDirectory(prefix="foliorun-session-demo-") as temporary:
        store = LocalArtifactStore(Path(temporary), "fictional-demo")
        profile = Profile(
            name="fictional-demo",
            version="1",
            image="localhost/demo@sha256:" + "a" * 64,
            image_manifest="b" * 64,
            skills_manifest="c" * 64,
            allowed_tools=(),
            sandbox="synthetic-demo",
            sdk_version="1",
        )
        tools, bindings = ToolRegistry("fictional-empty", "1", release="1"), WorkerBindings()
        bindings.install_tools(tools)
        provider = SyntheticProvider()
        harness = PydanticHarness(model_factory=lambda: (SyntheticClient(), FunctionModel(respond)))
        executor = Executor(store=store, bindings=bindings, sandbox=provider, harness=harness)
        scheduler = SyntheticScheduler(executor)
        runtime = Runtime(config=RuntimeConfig(profile, scheduler), bindings=bindings, store=store)
        first = await runtime.run(
            prompt="Entirely fictional first turn",
            vfs=MemoryFS({"/documents/specimen.txt": b"Entirely fictional original input"}),
            tools=tools,
            idempotency_key="first",
            session_id="fictional-session",
        )
        assert first.status == "SUCCEEDED", first.error
        second = await runtime.run(
            prompt="Entirely fictional follow-up",
            vfs=MemoryFS({}),
            tools=tools,
            idempotency_key="second",
            session_id="fictional-session",
            session_revision=1,
        )
        assert second.status == "SUCCEEDED", second.error
        assert len(provider.boxes) == 2
        assert (await runtime.checkpoint(second.handle)).revision == 2
        await runtime.artifacts.download_to(second.artifacts[0], Path(temporary) / "result.txt")
        print("offline session: 2 revisions, 2 fresh sandboxes, original input and native history")


if __name__ == "__main__":
    asyncio.run(main())
