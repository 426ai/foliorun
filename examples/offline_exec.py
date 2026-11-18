"""Execute a caller-bound sandbox tool and collect its output without network or credentials."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from offline_session import SyntheticBox, SyntheticProvider, SyntheticScheduler
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from foliorun import (
    LocalArtifactStore,
    MemoryFS,
    Profile,
    Runtime,
    RuntimeConfig,
    SandboxCommand,
    ToolRegistry,
    ToolSpec,
    WorkerBindings,
)
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.execution import Executor


class UppercaseBox(SyntheticBox):
    async def prepare(self, files, store, profile):
        assert profile.image == "tools.example.test/demo:uppercase"

    async def execute(self, command, args, call_id, limits):
        assert command.entrypoint == "uppercase-file"
        (self.root / "result.txt").write_text(args["text"].upper() + "\n", encoding="utf-8")
        return {"kind": "text", "value": "Wrote /output/result.txt"}


class UppercaseSandbox(SyntheticProvider):
    async def create(self, profile, run_id, scope):
        box = UppercaseBox()
        self.boxes.append(box)
        return box


def respond(messages, info):
    part = messages[-1].parts[0]
    if part.part_kind == "user-prompt":
        return ModelResponse(parts=[
            ToolCallPart("write_uppercase", {"text": "Fictional specimen"}, "write")
        ])
    if part.part_kind == "tool-return" and part.tool_name == "write_uppercase":
        return ModelResponse(parts=[
            ToolCallPart("submit_result", {"files": ["/output/result.txt"]}, "submit")
        ])
    return ModelResponse(parts=[TextPart("Submitted")])


async def main():
    with TemporaryDirectory(prefix="foliorun-exec-demo-") as temporary:
        sandbox = UppercaseSandbox()
        profile = Profile(
            name="fictional-exec",
            version="1",
            model="fictional-uppercase",
            image="tools.example.test/demo:uppercase",
            sandbox=sandbox.descriptor.name,
            sdk_version=sandbox.descriptor.version,
            allowed_tools=("write_uppercase",),
        )
        tools = ToolRegistry("fictional-uppercase", "1", release="1")
        tools.register(
            ToolSpec(
                name="write_uppercase",
                description="Write a fictional uppercase text file.",
                execution_location="sandbox",
                effect="workspace_write",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string", "maxLength": 128}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            ),
            SandboxCommand(entrypoint="uppercase-file"),
        )
        tools.seal()
        bindings = WorkerBindings()
        bindings.install_tools(tools)
        store = LocalArtifactStore(Path(temporary), "fictional-exec")
        harness = PydanticHarness(model=FunctionModel(respond, model_name=profile.model))
        executor = Executor(store=store, bindings=bindings, sandbox=sandbox, harness=harness)
        scheduler = SyntheticScheduler(executor)
        runtime = Runtime(config=RuntimeConfig(profile, scheduler), bindings=bindings, store=store)
        try:
            result = await runtime.run(
                prompt="Write and submit fictional uppercase text.",
                vfs=MemoryFS({}),
                tools=tools,
                idempotency_key="fictional-exec",
            )
            assert result.status == "SUCCEEDED", result.error
            assert len(result.artifacts) == 1
            assert store.read(result.artifacts[0].blob) == b"FICTIONAL SPECIMEN\n"
            print("offline exec: explicit sandbox tool ran; collected and verified one output")
        finally:
            for box in sandbox.boxes:
                await box.destroy()


if __name__ == "__main__":
    asyncio.run(main())
