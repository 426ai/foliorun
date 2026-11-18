"""Explicit host wiring. Importing this adapter neither connects nor starts a Worker."""

import asyncio
from contextlib import asynccontextmanager, suppress

from temporalio import activity
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from ..contracts import BlobRef, RuntimeFault
from .temporal_workflow import DocumentWorkflowV03 as DocumentWorkflow


class TemporalScheduler:
    def __init__(self, client, *, task_queue: str, hold_after_collect=False):
        self.client, self.task_queue = client, task_queue
        # Acceptance control only; normal runtime always finalizes immediately.
        self.hold_after_collect = hold_after_collect

    async def start(self, handle, job_ref):
        try:
            await self.client.start_workflow(
                DocumentWorkflow.run,
                {
                    "run_id": handle.run_id,
                    "job_ref": job_ref.model_dump(),
                    "hold_after_collect": self.hold_after_collect,
                },
                id="foliorun-" + handle.run_id,
                task_queue=self.task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        except WorkflowAlreadyStartedError:
            pass

    def workflow(self, handle):
        return self.client.get_workflow_handle("foliorun-" + handle.run_id)

    async def status(self, handle):
        return await self.workflow(handle).query(DocumentWorkflow.status)

    async def wait(self, handle, timeout):
        async with asyncio.timeout(timeout):
            return await self.workflow(handle).result()

    async def cancel(self, handle):
        return await self.workflow(handle).execute_update(DocumentWorkflow.cancel)

    async def collection_recovered(self, handle):
        await self.workflow(handle).signal(DocumentWorkflow.collection_recovered)


class TemporalActivities:
    def __init__(self, executor):
        self.executor = executor

    @activity.defn(name="foliorun-agent-v03")
    async def agent(self, request: dict) -> str:
        async def heartbeats():
            while True:
                activity.heartbeat("running")
                await asyncio.sleep(5)

        task = asyncio.create_task(heartbeats())
        try:
            try:
                await self.executor.execute(
                    request["run_id"], BlobRef.model_validate(request["job_ref"])
                )
            except RuntimeFault as exc:
                self.executor._failure(request["run_id"], exc.code, detail=exc.detail)
            except Exception:
                self.executor._failure(request["run_id"], "unknown_execution_window")
            return "receipt_persisted"
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @activity.defn(name="foliorun-fallback-v03")
    async def fallback(self, run_id: str) -> dict | None:
        return self.executor.reserve_fallback(run_id)

    @activity.defn(name="foliorun-collect-v03")
    async def collect(self, request: dict) -> str:
        try:
            await self.executor.recover_collection(request["run_id"], request["manifest_hash"])
        except RuntimeFault:
            raise
        except Exception:
            raise RuntimeFault("collection_unavailable") from None
        return "collection_receipt_persisted"

    @activity.defn(name="foliorun-finalize-v03")
    async def finalize(self, run_id: str) -> str:
        return self.executor.finalize(run_id)

    @activity.defn(name="foliorun-cancel-fence-v03")
    async def cancel(self, run_id: str) -> bool:
        return self.executor.cancel_fence(run_id)

    @asynccontextmanager
    async def worker(self, client, task_queue):
        # Agent capacity is independent from cancellation/publication capacity.
        async with Worker(
            client,
            task_queue=task_queue + "-control",
            activities=[self.finalize, self.cancel, self.fallback],
            max_concurrent_activities=2,
        ):
            async with Worker(
                client,
                task_queue=task_queue,
                workflows=[DocumentWorkflow],
                activities=[self.agent, self.finalize, self.cancel, self.fallback, self.collect],
                max_concurrent_activities=1,
            ) as agent_worker:
                yield agent_worker
