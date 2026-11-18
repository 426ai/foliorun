"""Minimal explicit assembly; only fictional inputs. Requires host environment credentials."""

import argparse
import asyncio
import os
from pathlib import Path
from uuid import uuid4

from bindings import document_tools
from temporalio.client import Client

from foliorun import (
    LocalArtifactStore,
    MemoryFS,
    Profile,
    Runtime,
    RuntimeConfig,
    WorkerBindings,
)
from foliorun.adapters.opensandbox import OpenSandboxProvider
from foliorun.adapters.pydantic_ai import PydanticHarness, country_only
from foliorun.adapters.temporal import TemporalActivities, TemporalScheduler
from foliorun.execution import Executor


async def main(args):
    await country_only()
    # Profile contains no credentials; use only explicitly selected provider keys.
    profile = Profile.model_validate_json(args.profile.read_bytes())
    store = LocalArtifactStore(args.data_dir, "fictional-example")
    tools, bindings = document_tools(), WorkerBindings()
    bindings.install_tools(tools)
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"], namespace=os.environ.get("TEMPORAL_NAMESPACE", "default")
    )
    scheduler = TemporalScheduler(client, task_queue="foliorun-synthetic-example")
    runtime = Runtime(config=RuntimeConfig(profile, scheduler), bindings=bindings, store=store)
    executor = Executor(
        store=store,
        bindings=bindings,
        sandbox=OpenSandboxProvider(
            api_key=os.environ["OPENSANDBOX_API_KEY"],
            endpoint=os.environ["OPENSANDBOX_ENDPOINT"],
        ),
        harness=PydanticHarness(openrouter_api_key=os.environ["OPENROUTER_API_KEY"]),
    )
    activities = TemporalActivities(executor)
    async with activities.worker(client, scheduler.task_queue):
        result = await runtime.run(
            prompt="Generate a DOCX containing 'Entirely fictional example' and submit the DOCX.",
            vfs=MemoryFS({}),
            tools=tools,
            idempotency_key=uuid4().hex,
        )
    print(result.status.value)
    if result.status == "SUCCEEDED":
        await runtime.artifacts.download_to(
            result.artifacts[0], Path("synthetic-example.docx"), expires_at=result.expires_at
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--allow-live", action="store_true")
    options = parser.parse_args()
    if not options.allow_live:
        parser.error("--allow-live is required: creates a sandbox and calls a paid model")
    asyncio.run(main(options))
