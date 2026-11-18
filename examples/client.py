"""Minimal explicit assembly; only fictional inputs. Requires host environment credentials."""

import argparse
import asyncio
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from bindings import document_sandbox, document_tools
from openai import AsyncOpenAI
from opensandbox.config import ConnectionConfig
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from temporalio.client import Client

from foliorun import (
    LocalArtifactStore,
    MemoryFS,
    Profile,
    Runtime,
    RuntimeConfig,
    WorkerBindings,
)
from foliorun.adapters.pydantic_ai import PydanticHarness
from foliorun.adapters.temporal import TemporalActivities, TemporalScheduler
from foliorun.execution import Executor


async def main(args):
    profile = Profile.model_validate_json(args.profile.read_bytes())
    store = LocalArtifactStore(args.data_dir, "fictional-example")
    tools, bindings = document_tools(), WorkerBindings()
    bindings.install_tools(tools)
    sandbox = document_sandbox(
        ConnectionConfig(
            api_key=os.environ["OPENSANDBOX_API_KEY"],
            domain=os.environ["OPENSANDBOX_ENDPOINT"],
            protocol=os.environ["OPENSANDBOX_PROTOCOL"],
            use_server_proxy=True,
            request_timeout=timedelta(seconds=140),
        ),
        owner=os.environ["OPENSANDBOX_OWNER"],
        skills_sha256=os.environ["DOCUMENT_SKILLS_SHA256"],
    )
    model_key, model_url = os.environ["MODEL_API_KEY"], os.environ["MODEL_BASE_URL"]
    if not model_key.strip() or not model_url.strip():
        raise ValueError("MODEL_API_KEY and MODEL_BASE_URL must be set explicitly")
    client = await Client.connect(
        os.environ["TEMPORAL_ADDRESS"], namespace=os.environ["TEMPORAL_NAMESPACE"]
    )
    scheduler = TemporalScheduler(client, task_queue=os.environ["TEMPORAL_TASK_QUEUE"])
    runtime = Runtime(config=RuntimeConfig(profile, scheduler), bindings=bindings, store=store)
    async with AsyncOpenAI(api_key=model_key, base_url=model_url, max_retries=0) as model_client:
        model = OpenAIChatModel(profile.model, provider=OpenAIProvider(openai_client=model_client))
        executor = Executor(
            store=store, bindings=bindings, sandbox=sandbox, harness=PydanticHarness(model=model)
        )
        activities = TemporalActivities(executor)
        async with activities.worker(client, scheduler.task_queue):
            result = await runtime.run(
                prompt="Generate a DOCX containing 'Entirely fictional example' and submit it.",
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
