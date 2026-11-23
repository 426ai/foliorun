"""Inject two independent model/image choices using only in-process synthetic transports."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from offline_session import SyntheticProvider, SyntheticScheduler
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
from foliorun.execution import Executor


class RecordingSandbox(SyntheticProvider):
    async def create(self, profile, run_id, scope):
        self.image_seen = profile.image
        return await super().create(profile, run_id, scope)


def respond(messages, info):
    assert any(tool.name == "submit_result" for tool in info.function_tools)
    if messages[-1].parts[0].part_kind == "user-prompt":
        return ModelResponse(
            parts=[ToolCallPart("submit_result", {"files": ["/output/result.txt"]}, "submit")]
        )
    return ModelResponse(parts=[TextPart("Submitted")])


async def run_example(root, binding_name, model_name, image_ref, *, use_factory):
    profile = Profile(
        name=binding_name,
        version="1",
        model=model_name,
        image=image_ref,
        sandbox=RecordingSandbox.descriptor.name,
        sdk_version=RecordingSandbox.descriptor.version,
        allowed_tools=(),
    )

    def create_model():
        return FunctionModel(respond, model_name=model_name)

    harness = (
        PydanticHarness(model_factory=create_model)
        if use_factory else PydanticHarness(model=create_model())
    )
    sandbox = RecordingSandbox()
    store = LocalArtifactStore(root, binding_name)
    bindings = WorkerBindings()
    tools = ToolRegistry("fictional-empty", "1", release="1")
    bindings.install_tools(tools)
    executor = Executor(store=store, bindings=bindings, sandbox=sandbox, harness=harness)
    scheduler = SyntheticScheduler(executor)
    runtime = Runtime(config=RuntimeConfig(profile, scheduler), bindings=bindings, store=store)
    result = await runtime.run(
        prompt="Submit the fictional tool output.",
        vfs=MemoryFS({}),
        tools=tools,
        idempotency_key="fictional-request",
    )
    assert result.status == "SUCCEEDED", result.error
    assert sandbox.image_seen == image_ref
    assert store.read(result.artifacts[0].blob) == b"Entirely fictional submitted file\n"
    print(f"{binding_name}: injected {model_name} and {image_ref}; collected one file")


async def main():
    with TemporaryDirectory(prefix="foliorun-injection-") as temporary:
        root = Path(temporary)
        await run_example(
            root, "cedar", "fictional-alpha", "tools.example.test/demo:cedar", use_factory=False
        )
        await run_example(root, "birch", "fictional-beta", "sha256:" + "b" * 64, use_factory=True)


if __name__ == "__main__":
    asyncio.run(main())
